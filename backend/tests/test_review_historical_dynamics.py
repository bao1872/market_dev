"""Tests for Analysis B Historical Dynamics Velocity / Signal / Acceleration /
Persistence.

Covers the frozen PRD §7.9 contracts:

12. EMA core: alpha exact, first-valid seed, warmup, recursive formula exact,
    incremental (reference oracle) equivalence.
13. Missing / gap: unavailable preserves state, insufficient day never advances
    the clock, consecutive gaps never decay/reset, dates never compressed.
14. Status propagation: PRD deterministic examples A-E (upstream status drives
    downstream status; ``value is None`` is never an availability cause).
15. Future leakage: T+1 / T+2 mutations never move EMA5/EMA20/Velocity/Signal/
    Acceleration/Persistence at T.
16. Real Position series: first-ready thresholds on a series produced by the
    real canonical producer (Position -> EMA5 -> EMA20 -> Velocity -> Signal ->
    Acceleration -> Persistence).
17. Persistence: PRD deterministic cases A-G, window metadata, boundary
    inclusivity, contract-violation fail-fast, historical gaps occupying
    window slots, no future leakage, no caller-overridable contract params.
"""

from __future__ import annotations

import inspect
import math
from datetime import date, timedelta
from typing import Any

import pytest

import app.domain.review.analysis.historical_dynamics as historical_dynamics_module
from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.analysis.historical_dynamics import (
    EMA_FAST_SPAN,
    EMA_SLOW_SPAN,
    LOWER_POSITION_THRESHOLD,
    PERSISTENCE_MINIMUM_VALID_COUNT,
    PERSISTENCE_WINDOW_SIZE,
    SIGNAL_SPAN,
    UPPER_POSITION_THRESHOLD,
    compute_ema_series,
    compute_historical_dynamics,
    compute_historical_dynamics_series,
    compute_persistence_series,
)
from app.domain.review.analysis.historical_position import compute_position_series
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


def _real_series_item(
    trade_date: date,
    *,
    ret: float,
    amount: float = 1e6,
    regime: float = 0.5,
    vol20: float = 1.0,
    vol200: float = 2.0,
) -> dict:
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


def _real_series(days: list[date], rets: list[float]) -> list[dict]:
    assert len(days) == len(rets)
    return [_real_series_item(d, ret=r) for d, r in zip(days, rets, strict=True)]


def _position_fact(
    td: date,
    position: float | None,
    status: str,
    *,
    primitive: str = "equal_weight_return",
) -> dict[str, Any]:
    """A Position fact in the exact shape produced by ``compute_position_series``
    (real field structure; status/value are caller-controlled for propagation)."""
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


def _ready_position_series(days: list[date], base: float = 10.0) -> list[dict]:
    """A series of N ``ready`` Position facts with finite values."""
    return [_position_fact(d, base + i, "ready") for i, d in enumerate(days)]


def _ema_input(items: list[tuple[str, float | None, str]]) -> list[dict]:
    """Build the generic EMA input shape from (trade_date, value, status)."""
    return [{"trade_date": td, "value": value, "status": status} for td, value, status in items]


class _EmaOracle:
    """Reference step-by-step frozen EMA (incremental state machine).

    The single source of the frozen numeric contract used to verify the batch
    owner: alpha = 2/(span+1), first-valid seed, warmup, state-preserve gaps.
    """

    def __init__(self, span: int) -> None:
        self.alpha = 2.0 / (span + 1.0)
        self.span = span
        self.state: float | None = None
        self.valid_count = 0

    def step(self, value: float | None, status: str) -> tuple[float | None, str, int]:
        if status == "unavailable_current":
            return (None, "unavailable_current", self.valid_count)
        if status == "insufficient_history":
            return (None, "insufficient_history", self.valid_count)
        if status == "ready":
            assert value is not None and math.isfinite(value)
            self.state = (
                value
                if self.state is None
                else self.alpha * value + (1.0 - self.alpha) * self.state
            )
            self.valid_count += 1
            if self.valid_count >= self.span:
                return (self.state, "ready", self.valid_count)
            return (None, "insufficient_history", self.valid_count)
        raise AssertionError(f"unknown status: {status!r}")


# ---------------------------------------------------------------------------
# Test 1 — alpha exact
# ---------------------------------------------------------------------------


def test_alpha_exact() -> None:
    """span=5 -> alpha 1/3; span=20 -> alpha 2/21 (checked via state update)."""
    # N zeros then a 1: the (N+1)-th output equals alpha (seed stays 0).
    zero5 = _ema_input([(f"2026-08-{i + 1:02d}", 0.0, "ready") for i in range(5)])
    out5 = compute_ema_series(
        zero5 + [{"trade_date": "2026-08-06", "value": 1.0, "status": "ready"}], span=5
    )
    assert out5[-1]["value"] == pytest.approx(1.0 / 3)

    zero20 = _ema_input([(f"2026-08-{i + 1:02d}", 0.0, "ready") for i in range(20)])
    out20 = compute_ema_series(
        zero20 + [{"trade_date": "2026-08-21", "value": 1.0, "status": "ready"}], span=20
    )
    assert out20[-1]["value"] == pytest.approx(2.0 / 21)


# ---------------------------------------------------------------------------
# Test 2 — first-valid seed
# ---------------------------------------------------------------------------


def test_first_valid_seed() -> None:
    """The first valid input seeds the internal state (never a 0 default)."""
    days = _trading_days(date(2026, 1, 5), 21)
    items = [{"trade_date": d.isoformat(), "value": 5.0, "status": "ready"} for d in days]
    out = compute_ema_series(items, span=20)
    # 20th valid output equals the seeded state (constant input stays 5.0).
    assert out[19]["status"] == "ready"
    assert out[19]["value"] == pytest.approx(5.0)
    # A later jump is blended from the seeded state, not from 0:
    #  state = (2/21)*7 + (19/21)*5.
    out21 = compute_ema_series(
        items[:20] + [{"trade_date": "2026-02-01", "value": 7.0, "status": "ready"}], span=20
    )
    assert out21[-1]["value"] == pytest.approx((2 / 21) * 7 + (19 / 21) * 5)


# ---------------------------------------------------------------------------
# Test 3 / 4 — warmup
# ---------------------------------------------------------------------------


def test_warmup_ema5() -> None:
    """EMA5: outputs 1-4 valid are None/insufficient; the 5th is first ready."""
    items = _ema_input([(f"2026-08-{i + 1:02d}", float(i), "ready") for i in range(6)])
    out = compute_ema_series(items, span=5)
    for i in range(4):
        assert out[i]["status"] == "insufficient_history"
        assert out[i]["value"] is None
    assert out[4]["status"] == "ready"
    assert out[4]["value"] is not None


def test_warmup_ema20() -> None:
    """EMA20: the 19th valid output is None; the 20th is first ready."""
    items = _ema_input([(f"2026-08-{i + 1:02d}", float(i), "ready") for i in range(21)])
    out = compute_ema_series(items, span=20)
    assert out[18]["status"] == "insufficient_history"
    assert out[18]["value"] is None
    assert out[19]["status"] == "ready"
    assert out[19]["value"] is not None


# ---------------------------------------------------------------------------
# Test 5 / 6 — recursive formula exact + incremental equivalence (oracle)
# ---------------------------------------------------------------------------


def _mixed_sequence() -> list[tuple[str, float | None, str]]:
    return [
        ("2026-08-01", 1.0, "ready"),
        ("2026-08-02", 2.0, "ready"),
        ("2026-08-03", None, "unavailable_current"),
        ("2026-08-04", 3.0, "ready"),
        ("2026-08-05", 4.0, "ready"),
        ("2026-08-06", 5.0, "ready"),
        ("2026-08-07", 6.0, "ready"),
        ("2026-08-08", None, "insufficient_history"),
        ("2026-08-09", 7.0, "ready"),
        ("2026-08-10", 8.0, "ready"),
        ("2026-08-11", 9.0, "ready"),
        ("2026-08-12", 10.0, "ready"),
        ("2026-08-13", 11.0, "ready"),
        ("2026-08-14", 12.0, "ready"),
        ("2026-08-15", 13.0, "ready"),
        ("2026-08-16", None, "unavailable_current"),
        ("2026-08-17", None, "unavailable_current"),
        ("2026-08-18", 14.0, "ready"),
        ("2026-08-19", 15.0, "ready"),
        ("2026-08-20", 16.0, "ready"),
        ("2026-08-21", 17.0, "ready"),
        ("2026-08-22", 18.0, "ready"),
        ("2026-08-23", 19.0, "ready"),
        ("2026-08-24", 20.0, "ready"),
    ]


@pytest.mark.parametrize("span", [5, 20])
def test_recursive_formula_matches_oracle(span: int) -> None:
    """Batch EMA output matches the reference step-by-step oracle exactly."""
    seq = _mixed_sequence()
    out = compute_ema_series(_ema_input(seq), span=span)
    oracle = _EmaOracle(span)
    expected = [oracle.step(value, status) for _, value, status in seq]
    assert [(o["value"], o["status"], o["valid_count"]) for o in out] == expected


@pytest.mark.parametrize("span", [5, 20])
def test_incremental_equivalence(span: int) -> None:
    """Feeding in two batches (state carried across) equals whole-batch compute."""
    seq = _mixed_sequence()
    oracle = _EmaOracle(span)
    results: list[tuple[float | None, str, int]] = []
    for batch in (seq[:7], seq[7:]):
        for _, value, status in batch:
            results.append(oracle.step(value, status))
    whole = compute_ema_series(_ema_input(seq), span=span)
    assert [(o["value"], o["status"], o["valid_count"]) for o in whole] == results


# ---------------------------------------------------------------------------
# Test 7 — ready -> unavailable_current -> ready (state preserved)
# ---------------------------------------------------------------------------


def test_gap_single_unavailable_preserves_state() -> None:
    """A missing day outputs None/unavailable_current and keeps the internal
    state; the next valid input resumes from the pre-gap state."""
    seq = [
        ("2026-08-01", 1.0, "ready"),
        ("2026-08-02", 2.0, "ready"),
        ("2026-08-03", 3.0, "ready"),
        ("2026-08-04", 4.0, "ready"),
        ("2026-08-05", 5.0, "ready"),  # EMA5 warm
        ("2026-08-06", None, "unavailable_current"),
        ("2026-08-07", 6.0, "ready"),
    ]
    out = compute_ema_series(_ema_input(seq), span=5)
    gap = out[5]
    assert gap["status"] == "unavailable_current"
    assert gap["value"] is None
    assert gap["valid_count"] == 5  # clock did NOT advance
    # Resume equals the oracle run over the identical sequence (no decay).
    oracle = _EmaOracle(5)
    expected = [oracle.step(value, status) for _, value, status in seq]
    assert [(o["value"], o["status"], o["valid_count"]) for o in out] == expected
    # The resumed day uses the pre-gap state, i.e. is NOT recomputed from scratch.
    assert out[6]["valid_count"] == 6


# ---------------------------------------------------------------------------
# Test 8 — insufficient_history day never advances the clock
# ---------------------------------------------------------------------------


def test_insufficient_day_does_not_advance_clock() -> None:
    """An insufficient_history input (ready state absent) neither updates state
    nor advances valid_count."""
    seq = [
        ("2026-08-01", 1.0, "ready"),
        ("2026-08-02", None, "insufficient_history"),
        ("2026-08-03", 2.0, "ready"),
        ("2026-08-04", None, "insufficient_history"),
        ("2026-08-05", 3.0, "ready"),
        ("2026-08-06", 4.0, "ready"),
        ("2026-08-07", 5.0, "ready"),
    ]
    out = compute_ema_series(_ema_input(seq), span=5)
    assert out[1]["valid_count"] == 1  # insufficient day: clock stays at 1
    assert out[3]["valid_count"] == 2  # insufficient day: clock stays at 2
    # Only the ready days are counted: 1 (d1), 2 (d3), 3 (d5), 4 (d6) -> warmup
    # still unmet at d6; d7 is the 5th valid -> ready.
    assert out[5]["valid_count"] == 4
    assert out[5]["status"] == "insufficient_history"
    assert out[6]["valid_count"] == 5
    assert out[6]["status"] == "ready"


# ---------------------------------------------------------------------------
# Test 9 — consecutive missing: no decay, no reset
# ---------------------------------------------------------------------------


def test_consecutive_gaps_no_decay_no_reset() -> None:
    """Three consecutive unavailable days neither decay nor reset the EMA."""
    seq = [
        ("2026-08-01", 10.0, "ready"),
        ("2026-08-02", 10.0, "ready"),
        ("2026-08-03", 10.0, "ready"),
        ("2026-08-04", 10.0, "ready"),
        ("2026-08-05", 10.0, "ready"),
        ("2026-08-06", None, "unavailable_current"),
        ("2026-08-07", None, "unavailable_current"),
        ("2026-08-08", None, "unavailable_current"),
        ("2026-08-09", 11.0, "ready"),
    ]
    out = compute_ema_series(_ema_input(seq), span=5)
    for i in (5, 6, 7):
        assert out[i]["status"] == "unavailable_current"
        assert out[i]["value"] is None
        assert out[i]["valid_count"] == 5  # clock frozen through the gap
    # Same output as a state-machine oracle with no decay/reset.
    oracle = _EmaOracle(5)
    expected = [oracle.step(value, status) for _, value, status in seq]
    assert [(o["value"], o["status"], o["valid_count"]) for o in out] == expected


# ---------------------------------------------------------------------------
# Test 10 — missing dates retained in output (never compressed)
# ---------------------------------------------------------------------------


def test_output_dates_never_compressed() -> None:
    """Every input day, including missing days, stays in the output series."""
    seq = [
        ("2026-08-01", 1.0, "ready"),
        ("2026-08-02", None, "unavailable_current"),
        ("2026-08-03", 2.0, "ready"),
    ]
    out = compute_ema_series(_ema_input(seq), span=5)
    assert [o["trade_date"] for o in out] == [d for d, _, _ in seq]
    assert len(out) == len(seq)


# ---------------------------------------------------------------------------
# Test 14 — status propagation: PRD deterministic examples A-E
# ---------------------------------------------------------------------------


def _run_chain(position_series: list[dict]) -> dict[str, list[dict]]:
    return compute_historical_dynamics_series(position_series)


def test_propagation_example_a_and_e() -> None:
    """A) Position+EMA5 ready, EMA20 insufficient -> Velocity insufficient_history.
    E) Velocity insufficient_history -> Signal insufficient_history (never
    unavailable_current merely because Velocity.value is None)."""
    days = _trading_days(date(2026, 1, 5), 12)
    series = _ready_position_series(days)
    chain = _run_chain(series)
    t = 10  # 11 ready days: EMA5 ready (>=5), EMA20 insufficient (<20)
    assert chain["position"][t]["status"] == "ready"
    assert chain["ema5"][t]["status"] == "ready"
    assert chain["ema20"][t]["status"] == "insufficient_history"
    assert chain["velocity"][t]["status"] == "insufficient_history"
    assert chain["velocity"][t]["value"] is None
    # E) the derived downstream stays insufficient, not unavailable_current.
    assert chain["signal"][t]["status"] == "insufficient_history"
    assert chain["acceleration"][t]["status"] == "insufficient_history"


def test_propagation_example_b() -> None:
    """B) Position unavailable_current -> EMA5/EMA20 unavailable_current ->
    Velocity unavailable_current (and downstream)."""
    days = _trading_days(date(2026, 1, 5), 7)
    series = _ready_position_series(days[:6]) + [
        _position_fact(days[6], None, "unavailable_current")
    ]
    chain = _run_chain(series)
    t = 6
    assert chain["position"][t]["status"] == "unavailable_current"
    assert chain["ema5"][t]["status"] == "unavailable_current"
    assert chain["ema20"][t]["status"] == "unavailable_current"
    assert chain["velocity"][t]["status"] == "unavailable_current"
    assert chain["signal"][t]["status"] == "unavailable_current"
    assert chain["acceleration"][t]["status"] == "unavailable_current"
    # Values are null; the status (not the null) is the availability cause.
    assert chain["velocity"][t]["value"] is None


def test_propagation_example_c() -> None:
    """C) Velocity ready + Signal insufficient_history -> Acceleration
    insufficient_history."""
    days = _trading_days(date(2026, 1, 5), 24)
    series = _ready_position_series(days)
    chain = _run_chain(series)
    t = 19  # EMA20 ready here (20 ready days) -> Velocity ready; Signal warmup <5
    assert chain["velocity"][t]["status"] == "ready"
    assert chain["signal"][t]["status"] == "insufficient_history"
    assert chain["acceleration"][t]["status"] == "insufficient_history"
    assert chain["acceleration"][t]["value"] is None


def test_propagation_example_d() -> None:
    """D) Velocity unavailable_current -> Signal unavailable_current ->
    Acceleration unavailable_current."""
    days = _trading_days(date(2026, 1, 5), 25)
    series = _ready_position_series(days[:24]) + [
        _position_fact(days[24], None, "unavailable_current")
    ]
    chain = _run_chain(series)
    t = 24
    assert chain["velocity"][t]["status"] == "unavailable_current"
    assert chain["signal"][t]["status"] == "unavailable_current"
    assert chain["acceleration"][t]["status"] == "unavailable_current"


# ---------------------------------------------------------------------------
# Test 15 — future leakage (golden)
# ---------------------------------------------------------------------------


def test_future_leakage_golden() -> None:
    """Mutating T+1 / T+2 payloads leaves EMA5/EMA20/Velocity/Signal/
    Acceleration at T bit-identical."""
    days = _trading_days(date(2026, 1, 5), 130)
    rets = [0.01 * (i % 7) for i in range(130)]
    series = _real_series(days, rets)
    t_idx = 90  # fully warmed: position+EMA5+EMA20+Velocity+Signal+Acceleration

    base_pos = compute_position_series(series, "equal_weight_return")
    base = compute_historical_dynamics_series(base_pos)

    mutated = list(series)
    mutated[91] = _real_series_item(days[91], ret=1e9)
    mutated[92] = _real_series_item(days[92], ret=-1e9)
    after_pos = compute_position_series(mutated, "equal_weight_return")
    after = compute_historical_dynamics_series(after_pos)

    for fact_key in (
        "position",
        "ema5",
        "ema20",
        "velocity",
        "signal",
        "acceleration",
        "persistence",
    ):
        assert after[fact_key][t_idx] == base[fact_key][t_idx], fact_key


# ---------------------------------------------------------------------------
# Test 16 — first-ready thresholds on a real Position series
# ---------------------------------------------------------------------------


def test_first_ready_thresholds_real_series() -> None:
    """On a real canonical series, Position -> EMA5 -> EMA20 -> Velocity ->
    Signal -> Acceleration become ready in the frozen order and at the exact
    observation counts (Position 60 / EMA5 5 / EMA20 20 / Velocity 20 /
    Signal 5 valid Velocity / Acceleration same as Signal)."""
    days = _trading_days(date(2026, 1, 5), 130)
    rets = [0.01 * (i % 7) for i in range(130)]
    series = _real_series(days, rets)
    pos = compute_position_series(series, "equal_weight_return")
    chain = compute_historical_dynamics_series(pos)

    def _first_ready(series_of_facts: list[dict], value_field: str) -> int:
        for i, fact in enumerate(series_of_facts):
            if fact["status"] == "ready" and fact[value_field] is not None:
                return i
        raise AssertionError("no ready fact")

    assert _first_ready(chain["position"], "position") == 60  # 60 pre-T baseline
    assert _first_ready(chain["ema5"], "value") == 64  # 5th ready Position
    assert _first_ready(chain["ema20"], "value") == 79  # 20th ready Position
    assert _first_ready(chain["velocity"], "value") == 79
    assert _first_ready(chain["signal"], "value") == 83  # 5th valid Velocity
    assert _first_ready(chain["acceleration"], "value") == 83
    # At first Velocity-ready, Position/EMA5/EMA20 are all ready.
    for key in ("position", "ema5", "ema20"):
        assert chain[key][79]["status"] == "ready"


# ---------------------------------------------------------------------------
# Multi-primitive wrapper + determinism + no mutation
# ---------------------------------------------------------------------------


def test_multi_primitive_loop_uses_position_keys() -> None:
    """The thin wrapper loops over the given Position series keys (inheriting
    the 11 historical-ready primitive keys) without a second registry."""
    days = _trading_days(date(2026, 1, 5), 130)
    rets = [0.01 * (i % 7) for i in range(130)]
    series = _real_series(days, rets)
    positions = {
        key: compute_position_series(series, key)
        for key in ("equal_weight_return", "advance_ratio")
    }
    dynamics = compute_historical_dynamics(positions)
    assert set(dynamics) == {"equal_weight_return", "advance_ratio"}
    for key in ("equal_weight_return", "advance_ratio"):
        for fact_key in (
            "position",
            "ema5",
            "ema20",
            "velocity",
            "signal",
            "acceleration",
            "persistence",
        ):
            assert len(dynamics[key][fact_key]) == len(days), (key, fact_key)


def test_deterministic_and_no_mutation() -> None:
    """Repeated execution is identical and the input Position series is not
    mutated."""
    days = _trading_days(date(2026, 1, 5), 130)
    rets = [0.01 * (i % 7) for i in range(130)]
    series = _real_series(days, rets)
    pos = compute_position_series(series, "equal_weight_return")
    snapshot = [dict(f) for f in pos]
    a = compute_historical_dynamics_series(pos)
    b = compute_historical_dynamics_series(pos)
    assert a == b
    assert pos == snapshot


def test_ema_owner_validates_ascending_and_status() -> None:
    """Fail fast on non-ascending trade dates and on unknown status values."""
    items = _ema_input([("2026-08-02", 1.0, "ready"), ("2026-08-01", 2.0, "ready")])
    with pytest.raises(ValueError, match="ascending"):
        compute_ema_series(items, span=5)
    with pytest.raises(ValueError, match="unknown upstream status"):
        compute_ema_series([{"trade_date": "2026-08-01", "value": 1.0, "status": "nope"}], span=5)
    with pytest.raises(ValueError, match="ready with non-finite"):
        compute_ema_series([{"trade_date": "2026-08-01", "value": None, "status": "ready"}], span=5)


def test_frozen_spans_constants() -> None:
    """The frozen PRD span constants are the canonical numbers."""
    assert EMA_FAST_SPAN == 5
    assert EMA_SLOW_SPAN == 20
    assert SIGNAL_SPAN == 5


# ---------------------------------------------------------------------------
# Frozen-span contract enforcement (remote-audit correction round)
# ---------------------------------------------------------------------------


def test_series_api_signature_has_no_span_parameters() -> None:
    """Test A: the canonical series API must NOT expose span_fast / span_slow."""
    params = list(inspect.signature(compute_historical_dynamics_series).parameters)
    assert "span_fast" not in params
    assert "span_slow" not in params


def test_multi_api_signature_has_no_span_parameters() -> None:
    """Test B: the canonical multi-primitive API must NOT expose
    span_fast / span_slow."""
    params = list(inspect.signature(compute_historical_dynamics).parameters)
    assert "span_fast" not in params
    assert "span_slow" not in params


def test_product_owner_consumes_frozen_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test C: spy on the generic EMA owner and prove the product chain actually
    consumes Fast=5, Slow=20, Signal=5 in that order — not just the constants."""
    calls: list[int] = []
    original = historical_dynamics_module.compute_ema_series

    def _spy(input_series: Any, span: int) -> list[dict[str, Any]]:
        calls.append(span)
        return original(input_series, span)

    monkeypatch.setattr(historical_dynamics_module, "compute_ema_series", _spy)

    days = _trading_days(date(2026, 1, 5), 130)
    rets = [0.01 * (i % 7) for i in range(130)]
    series = _real_series(days, rets)
    pos = compute_position_series(series, "equal_weight_return")
    compute_historical_dynamics_series(pos)
    # EMA5(Position) -> 5, EMA20(Position) -> 20, EMA5(Velocity) -> 5.
    assert calls == [EMA_FAST_SPAN, EMA_SLOW_SPAN, SIGNAL_SPAN]
    assert calls == [5, 20, 5]


# ---------------------------------------------------------------------------
# Test 17 — Persistence (PRD §7.9 Persistence Numerical Contract)
# ---------------------------------------------------------------------------


def test_persistence_frozen_constants() -> None:
    """The frozen PRD Persistence numbers are the canonical constants."""
    assert PERSISTENCE_WINDOW_SIZE == 20
    assert PERSISTENCE_MINIMUM_VALID_COUNT == 15
    assert UPPER_POSITION_THRESHOLD == 80.0
    assert LOWER_POSITION_THRESHOLD == 20.0


def test_persistence_output_shape_exact() -> None:
    """Persistence facts expose exactly the transparent metadata fields — no
    reason / label / phase / middle / score."""
    days = _trading_days(date(2026, 1, 5), 20)
    series = [_position_fact(d, 50.0, "ready") for d in days]
    fact = compute_persistence_series(series)[19]
    assert set(fact) == {
        "trade_date",
        "window_size",
        "minimum_valid_count",
        "candidate_count",
        "valid_count",
        "coverage",
        "upper_count",
        "lower_count",
        "upper_occupancy",
        "lower_occupancy",
        "status",
    }


def test_persistence_case_a() -> None:
    """20 slots / 20 valid / 5 upper / 5 lower -> ready, Upper=.25, Lower=.25."""
    days = _trading_days(date(2026, 1, 5), 20)
    positions = [50.0] * 20
    for i in range(5):
        positions[i] = 90.0  # upper
    for i in range(5, 10):
        positions[i] = 10.0  # lower
    series = [_position_fact(d, v, "ready") for d, v in zip(days, positions, strict=True)]
    fact = compute_persistence_series(series)[19]
    assert fact["status"] == "ready"
    assert fact["candidate_count"] == 20
    assert fact["valid_count"] == 20
    assert fact["coverage"] == pytest.approx(1.0)
    assert fact["upper_count"] == 5
    assert fact["lower_count"] == 5
    assert fact["upper_occupancy"] == pytest.approx(0.25)
    assert fact["lower_occupancy"] == pytest.approx(0.25)


def test_persistence_case_b() -> None:
    """16 valid (all >=80) + 4 historical missing, current T ready -> ready,
    Upper=1.0, Lower=0, coverage=.8 (no back-fill for the missing slots)."""
    days = _trading_days(date(2026, 1, 5), 20)
    series = []
    for i, d in enumerate(days):
        if i < 4:
            series.append(_position_fact(d, None, "insufficient_history"))
        else:
            series.append(_position_fact(d, 90.0, "ready"))
    fact = compute_persistence_series(series)[19]
    assert fact["status"] == "ready"
    assert fact["candidate_count"] == 20
    assert fact["valid_count"] == 16
    assert fact["coverage"] == pytest.approx(0.8)
    assert fact["upper_count"] == 16
    assert fact["lower_count"] == 0
    assert fact["upper_occupancy"] == pytest.approx(1.0)
    assert fact["lower_occupancy"] == pytest.approx(0.0)


def test_persistence_case_c() -> None:
    """14 valid + 6 historical missing, current T ready -> insufficient_history,
    Upper/Lower=None, valid_count=14, coverage=.7."""
    days = _trading_days(date(2026, 1, 5), 20)
    series = []
    for i, d in enumerate(days):
        if i < 6:
            series.append(_position_fact(d, None, "insufficient_history"))
        else:
            series.append(_position_fact(d, 50.0, "ready"))
    fact = compute_persistence_series(series)[19]
    assert fact["status"] == "insufficient_history"
    assert fact["upper_occupancy"] is None
    assert fact["lower_occupancy"] is None
    assert fact["valid_count"] == 14
    assert fact["coverage"] == pytest.approx(0.7)


def test_persistence_case_d() -> None:
    """19 valid candidates but Position(T)=unavailable_current ->
    unavailable_current, Upper/Lower=None; window metadata stays transparent."""
    days = _trading_days(date(2026, 1, 5), 20)
    series = [_position_fact(d, 50.0, "ready") for d in days[:19]] + [
        _position_fact(days[19], None, "unavailable_current")
    ]
    fact = compute_persistence_series(series)[19]
    assert fact["status"] == "unavailable_current"
    assert fact["upper_occupancy"] is None
    assert fact["lower_occupancy"] is None
    # Even though historical window coverage would be enough, no old
    # Persistence is emitted; metadata remains transparent.
    assert fact["candidate_count"] == 20
    assert fact["valid_count"] == 19
    assert fact["coverage"] == pytest.approx(0.95)


def test_persistence_case_e() -> None:
    """19 valid candidates but Position(T)=insufficient_history ->
    insufficient_history (current upstream status is not overridden by the
    19 other ready window positions)."""
    days = _trading_days(date(2026, 1, 5), 20)
    series = [_position_fact(d, 50.0, "ready") for d in days[:19]] + [
        _position_fact(days[19], None, "insufficient_history")
    ]
    fact = compute_persistence_series(series)[19]
    assert fact["status"] == "insufficient_history"
    assert fact["upper_occupancy"] is None
    assert fact["lower_occupancy"] is None
    assert fact["valid_count"] == 19


def test_persistence_case_f() -> None:
    """20 valid Positions all 50.0 -> ready, Upper=0, Lower=0 (Upper+Lower need
    NOT sum to 1)."""
    days = _trading_days(date(2026, 1, 5), 20)
    series = [_position_fact(d, 50.0, "ready") for d in days]
    fact = compute_persistence_series(series)[19]
    assert fact["status"] == "ready"
    assert fact["upper_count"] == 0
    assert fact["lower_count"] == 0
    assert fact["upper_occupancy"] == pytest.approx(0.0)
    assert fact["lower_occupancy"] == pytest.approx(0.0)


def test_persistence_case_g_short_series() -> None:
    """Series beginning with only 10 observations -> candidate_count=10,
    window_size=20, valid_count=10, coverage=.5, insufficient_history (never
    a fake candidate_count=20 / coverage=1.0)."""
    days = _trading_days(date(2026, 1, 5), 10)
    series = [_position_fact(d, 50.0, "ready") for d in days]
    fact = compute_persistence_series(series)[9]
    assert fact["window_size"] == 20
    assert fact["candidate_count"] == 10
    assert fact["valid_count"] == 10
    assert fact["coverage"] == pytest.approx(0.5)
    assert fact["status"] == "insufficient_history"
    assert fact["upper_occupancy"] is None
    assert fact["lower_occupancy"] is None


def test_persistence_boundaries_inclusive() -> None:
    """80.0 -> Upper, 20.0 -> Lower; 79.999 / 20.001 do NOT cross the boundary."""
    days = _trading_days(date(2026, 1, 5), 20)
    positions = [50.0] * 20
    positions[0] = 80.0
    positions[1] = 20.0
    positions[2] = 79.999
    positions[3] = 20.001
    series = [_position_fact(d, v, "ready") for d, v in zip(days, positions, strict=True)]
    fact = compute_persistence_series(series)[19]
    assert fact["status"] == "ready"
    assert fact["upper_count"] == 1  # only 80.0
    assert fact["lower_count"] == 1  # only 20.0
    assert fact["upper_occupancy"] == pytest.approx(1 / 20)
    assert fact["lower_occupancy"] == pytest.approx(1 / 20)


@pytest.mark.parametrize(
    "bad_position",
    [None, float("nan"), float("inf"), float("-inf"), -0.1, 100.1],
)
def test_persistence_contract_violation_fails_fast(bad_position: Any) -> None:
    """status=ready with a non-finite / out-of-range position is an upstream
    contract violation -> ValueError (never silently treated as missing)."""
    days = _trading_days(date(2026, 1, 5), 20)
    series = [_position_fact(d, 50.0, "ready") for d in days]
    series[10] = _position_fact(days[10], bad_position, "ready")
    with pytest.raises(ValueError):
        compute_persistence_series(series)


def test_persistence_non_ready_statuses_with_none_position_are_legal() -> None:
    """unavailable_current / insufficient_history with position=None are legal
    (occupy a slot) but never count as valid."""
    days = _trading_days(date(2026, 1, 5), 20)
    series = [_position_fact(d, 50.0, "ready") for d in days]
    series[5] = _position_fact(days[5], None, "unavailable_current")
    series[6] = _position_fact(days[6], None, "insufficient_history")
    fact = compute_persistence_series(series)[19]
    assert fact["status"] == "ready"
    assert fact["candidate_count"] == 20
    assert fact["valid_count"] == 18
    assert fact["upper_occupancy"] == pytest.approx(0.0)
    assert fact["lower_occupancy"] == pytest.approx(0.0)


def test_persistence_historical_gap_occupies_slot() -> None:
    """3 consecutive unavailable days inside the window: they still occupy the
    20-slot window, candidate_count is unchanged, valid_count drops by 3 and no
    back-fill is performed."""
    days = _trading_days(date(2026, 1, 5), 22)
    series = [_position_fact(d, 50.0, "ready") for d in days]
    for i in (14, 15, 16):
        series[i] = _position_fact(days[i], None, "unavailable_current")
    per = compute_persistence_series(series)
    for i in (14, 15, 16):
        assert per[i]["status"] == "unavailable_current"
    fact = per[21]
    assert fact["candidate_count"] == 20
    assert fact["valid_count"] == 17  # 20 slots - 3 gap days
    assert fact["status"] == "ready"
    assert fact["coverage"] == pytest.approx(0.85)


def test_persistence_future_leakage_direct() -> None:
    """Mutating T+1 / T+2 / T+3 Position facts (Position 0 / 100 / unavailable)
    leaves Persistence(T) bit-identical."""
    days = _trading_days(date(2026, 1, 5), 23)
    base = [_position_fact(d, 50.0, "ready") for d in days]
    t = 19
    snapshot = dict(compute_persistence_series(base)[t])
    mutated = list(base)
    mutated[20] = _position_fact(days[20], 0.0, "ready")
    mutated[21] = _position_fact(days[21], 100.0, "ready")
    mutated[22] = _position_fact(days[22], None, "unavailable_current")
    after = compute_persistence_series(mutated)
    assert after[t] == snapshot


def test_persistence_first_ready_real_series() -> None:
    """On a real canonical series, Position first becomes ready at the 61st
    observation (index 60); Persistence first becomes ready at the 15th ready
    Position (index 74), when the 20-slot window holds valid_count == 15."""
    days = _trading_days(date(2026, 1, 5), 130)
    rets = [0.01 * (i % 7) for i in range(130)]
    series = _real_series(days, rets)
    pos = compute_position_series(series, "equal_weight_return")
    per = compute_persistence_series(pos)
    assert pos[60]["status"] == "ready"
    assert pos[59]["status"] == "insufficient_history"
    assert per[73]["status"] == "insufficient_history"
    assert per[74]["status"] == "ready"
    assert per[74]["valid_count"] == 15
    assert per[74]["coverage"] == pytest.approx(0.75)


def test_persistence_derived_directly_from_position() -> None:
    """The integrated chain's persistence equals compute_persistence_series on
    the Position series alone (never derived from Velocity / Signal /
    Acceleration)."""
    days = _trading_days(date(2026, 1, 5), 130)
    rets = [0.01 * (i % 7) for i in range(130)]
    series = _real_series(days, rets)
    pos = compute_position_series(series, "equal_weight_return")
    chain = compute_historical_dynamics_series(pos)
    assert chain["persistence"] == compute_persistence_series(pos)
    assert len(chain["persistence"]) == len(days)


def test_persistence_empty_series() -> None:
    """Empty input -> empty output (no crash, no phantom facts)."""
    assert compute_persistence_series([]) == []


def test_persistence_fails_fast_on_non_ascending() -> None:
    """Duplicate / descending trade dates fail fast (never silently re-sorted)."""
    series = [
        _position_fact(date(2026, 8, 2), 50.0, "ready"),
        _position_fact(date(2026, 8, 1), 50.0, "ready"),
    ]
    with pytest.raises(ValueError, match="ascending"):
        compute_persistence_series(series)


def test_persistence_fails_fast_on_unknown_status() -> None:
    """Unknown upstream status fails fast."""
    series = [_position_fact(date(2026, 8, 1), 50.0, "nope")]
    with pytest.raises(ValueError, match="unknown upstream status"):
        compute_persistence_series(series)


def test_persistence_api_has_no_contract_overrides() -> None:
    """The canonical product API must NOT expose window / threshold overrides
    (frozen 20 / 15 / 80 / 20)."""
    params = list(inspect.signature(compute_persistence_series).parameters)
    assert "window_size" not in params
    assert "minimum_valid_count" not in params
    assert "upper_threshold" not in params
    assert "lower_threshold" not in params


def test_multi_primitive_persistence_date_aligned() -> None:
    """Every input primitive gets an equal-length, date-aligned persistence
    series in the multi-primitive wrapper (no second primitive registry)."""
    days = _trading_days(date(2026, 1, 5), 130)
    rets = [0.01 * (i % 7) for i in range(130)]
    series = _real_series(days, rets)
    positions = {
        key: compute_position_series(series, key)
        for key in ("equal_weight_return", "advance_ratio")
    }
    dynamics = compute_historical_dynamics(positions)
    for key in ("equal_weight_return", "advance_ratio"):
        per = dynamics[key]["persistence"]
        assert len(per) == len(days)
        assert [p["trade_date"] for p in per] == [
            p["trade_date"] for p in dynamics[key]["position"]
        ]
