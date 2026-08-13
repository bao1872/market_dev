"""Round 2A modified-scope pure/unit tests: Objective Evidence Engine.

Covers the pure evidence math (``scope_evidence``) and the thin service
(``scope_evidence_service``): percentile_rank, primitive extraction, bool/non-
finite rejection, delta, exact D1/D3/D5 via canonical calendar, missing-exact-date
-> unavailable (no fallback), historical min-60 gate, current-excluded history,
same-family peer, cross-family isolation, raw HHI peer disabled, no-subjective
keys, and L1 input not mutated.  No DB, no network, no CI.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.review.scope_evidence import (
    PRIMITIVE_NAMES,
    RAW_HHI_PEER_DISABLED_REASON,
    build_current_context,
    build_delta_context,
    build_historical_context,
    build_peer_context,
    compute_delta,
    extract_primitive,
    percentile_rank,
)
from app.services import scope_evidence_service
from app.services.scope_evidence_service import compute_scope_evidence

T = date(2026, 8, 11)

# canonical calendar: T->T1->... (weekends skipped as non-trading days)
_D1 = date(2026, 8, 10)
_D3 = date(2026, 8, 6)
_D5 = date(2026, 8, 4)


def _payload(
    *,
    price_return_mean: float | None = 0.01,
    price_advance_ratio: float | None = 0.5,
    trend_up_ratio: float | None = 0.31,
    momentum_expanding_ratio: float | None = 0.4,
    participation_volume_p50: float | None = 1.2,
    price_raw_hhi: float | None = 0.3,
) -> dict:
    """Build a canonical-shape payload with Phase-1 primitive values."""
    return {
        "scope": {"scope_type": "concept", "scope_key": "A", "trade_date": T.isoformat()},
        "price": {
            "return": {"mean": price_return_mean, "median": None},
            "breadth": {
                "advance_ratio": price_advance_ratio,
                "decline_ratio": None,
                "unchanged_ratio": None,
            },
            "concentration": {"raw_hhi": price_raw_hhi, "status": "ready"},
            "amount": {"valid_count": 1, "total_amount": 100.0, "concentration": {"raw_hhi": None, "status": "ready"}},
        },
        "trend": {"state": {"up_ratio": trend_up_ratio}, "transition": {}},
        "structure": {
            "swing": {"state": {"up_ratio": None, "down_ratio": None}, "transition": {}},
            "internal": {"state": {"up_ratio": None}, "transition": {}},
        },
        "momentum": {
            "state": {"expanding_ratio": momentum_expanding_ratio},
            "transition": {},
        },
        "participation": {"volume": {"p50": participation_volume_p50}, "amount": {}},
        "chip": {"status": "unavailable"},
    }


def _fact(
    trade_date: date,
    *,
    scope_type: str = "concept",
    scope_key: str = "A",
    payload: dict | None = None,
) -> SimpleNamespace:
    p = payload if payload is not None else _payload()
    return SimpleNamespace(
        trade_date=trade_date,
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name=None,
        pit_member_count=10,
        observation_payload=p,
    )


def _prev_map() -> dict[date, date]:
    """Canonical calendar mapping for the pure service test (no weekend dates)."""
    seq = [_D5, date(2026, 8, 5), _D3, date(2026, 8, 7), _D1, T]
    return {later: earlier for earlier, later in zip(seq[:-1], seq[1:], strict=True)}


async def _patch_all(monkeypatch, *, prev, get, list_):
    async def _prev(session, ref_date):
        return prev(ref_date)

    monkeypatch.setattr(
        scope_evidence_service.calendar_service,
        "get_previous_trading_day_async",
        _prev,
    )
    monkeypatch.setattr(scope_evidence_service, "get_scope_observation_fact", get)
    monkeypatch.setattr(scope_evidence_service, "list_scope_observation_facts", list_)


# ---------------------------------------------------------------------------
# A. percentile_rank basic
# ---------------------------------------------------------------------------


def test_percentile_rank_min() -> None:
    assert percentile_rank(1.0, [1.0, 2.0, 3.0, 4.0]) == 25.0


def test_percentile_rank_max() -> None:
    assert percentile_rank(4.0, [1.0, 2.0, 3.0, 4.0]) == 100.0


def test_percentile_rank_middle() -> None:
    assert percentile_rank(2.5, [1.0, 2.0, 3.0, 4.0]) == 50.0


# ---------------------------------------------------------------------------
# B. percentile_rank ties
# ---------------------------------------------------------------------------


def test_percentile_rank_ties_deterministic() -> None:
    assert percentile_rank(2.0, [1.0, 2.0, 2.0, 2.0, 3.0]) == 80.0


# ---------------------------------------------------------------------------
# C. NaN/inf/None filtering
# ---------------------------------------------------------------------------


def test_percentile_rank_filters_non_finite() -> None:
    assert percentile_rank(5.0, [1.0, 2.0, 3.0, float("inf"), float("nan")]) == 100.0


def test_percentile_rank_empty_after_filter_is_none() -> None:
    assert percentile_rank(1.0, []) is None
    assert percentile_rank(1.0, [float("nan"), float("inf")]) is None
    assert percentile_rank(float("nan"), [1.0, 2.0]) is None


# ---------------------------------------------------------------------------
# D. primitive extraction
# ---------------------------------------------------------------------------


def test_primitive_extraction_maps_paths() -> None:
    for prim in PRIMITIVE_NAMES:
        assert extract_primitive(_payload(), prim) is not None


def test_primitive_extraction_missing_path_none() -> None:
    assert extract_primitive(_payload(price_return_mean=None), "price_return_mean") is None
    assert extract_primitive({"price": {}}, "price_return_mean") is None


# ---------------------------------------------------------------------------
# E. bool rejection
# ---------------------------------------------------------------------------


def test_bool_is_rejected_as_numeric() -> None:
    assert extract_primitive(_payload(price_return_mean=True), "price_return_mean") is None
    assert extract_primitive(_payload(price_return_mean=False), "price_return_mean") is None


# ---------------------------------------------------------------------------
# F. delta
# ---------------------------------------------------------------------------


def test_delta_plain() -> None:
    assert compute_delta(0.31, 0.24) == pytest.approx(0.07)


def test_delta_unavailable_returns_none() -> None:
    assert compute_delta(None, 0.24) is None
    assert compute_delta(0.31, None) is None


def test_delta_context_shape() -> None:
    ctx = build_delta_context(0.31, 0.24, T)
    assert ctx["status"] == "ready"
    assert ctx["reference_date"] == T.isoformat()
    assert ctx["delta"] == pytest.approx(0.07)


# ---------------------------------------------------------------------------
# G/H/I. exact D1/D3/D5 via canonical calendar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d1_d3_d5_exact_dates(monkeypatch) -> None:
    prev = _prev_map()

    async def get(db, td, st, sk):
        return _fact(td, scope_type=st, scope_key=sk)

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    result = await compute_scope_evidence(AsyncMock(), T, "concept", "A")

    prim = result["primitives"]["trend_up_ratio"]
    assert prim["d1"]["status"] == "ready"
    assert prim["d1"]["reference_date"] == _D1.isoformat()
    assert prim["d3"]["reference_date"] == _D3.isoformat()
    assert prim["d5"]["reference_date"] == _D5.isoformat()


# ---------------------------------------------------------------------------
# J. missing exact date -> unavailable, no fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_exact_date_unavailable_no_fallback(monkeypatch) -> None:
    prev = _prev_map()

    async def get(db, td, st, sk):
        if td == _D3:  # exact D3 missing -> unavailable
            return None
        return _fact(td, scope_type=st, scope_key=sk)

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    result = await compute_scope_evidence(AsyncMock(), T, "concept", "A")

    prim = result["primitives"]["trend_up_ratio"]
    assert prim["d1"]["status"] == "ready"
    assert prim["d3"]["status"] == "unavailable"
    assert prim["d3"]["reference_value"] is None
    assert prim["d5"]["status"] == "ready"


# ---------------------------------------------------------------------------
# K. historical <60 insufficient ; L. >=60 ready
# ---------------------------------------------------------------------------


def test_historical_below_60_insufficient() -> None:
    samples = [float(i) for i in range(10)]
    ctx = build_historical_context(5.0, samples, date(2026, 1, 1), date(2026, 6, 1))
    assert ctx["status"] == "insufficient_history"
    assert ctx["percentile"] is None
    assert ctx["sample_count"] == 10


def test_historical_at_least_60_ready() -> None:
    samples = [float(i) for i in range(60)]
    ctx = build_historical_context(30.0, samples, date(2026, 1, 1), date(2026, 6, 1))
    assert ctx["status"] == "ready"
    assert ctx["percentile"] is not None
    assert ctx["sample_count"] == 60


# ---------------------------------------------------------------------------
# K2. historical status precedence (Round 2A correction)
#   A. current=None, history=5  -> unavailable (not insufficient_history)
#   B. current=None, history=60 -> unavailable
#   C. current valid, history=5 -> insufficient_history  (covered by K above)
#   D. current valid, history=60-> ready                 (covered by L above)
# ---------------------------------------------------------------------------


def test_historical_current_none_small_sample_unavailable() -> None:
    """A: current value None + history < 60 -> unavailable (not insufficient)."""
    samples = [float(i) for i in range(5)]
    ctx = build_historical_context(None, samples, date(2026, 1, 1), date(2026, 6, 1))
    assert ctx["status"] == "unavailable"
    assert ctx["percentile"] is None
    # sample metadata still preserved
    assert ctx["sample_count"] == 5
    assert ctx["history_start_date"] == "2026-01-01"
    assert ctx["history_end_date"] == "2026-06-01"


def test_historical_current_none_large_sample_unavailable() -> None:
    """B: current value None even with history >= 60 -> unavailable."""
    samples = [float(i) for i in range(60)]
    ctx = build_historical_context(None, samples, date(2026, 1, 1), date(2026, 6, 1))
    assert ctx["status"] == "unavailable"
    assert ctx["percentile"] is None
    assert ctx["sample_count"] == 60


# ---------------------------------------------------------------------------
# M. current excluded from historical sample (service-level)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_excluded_from_historical_sample(monkeypatch) -> None:
    prev = _prev_map()
    hist_payloads = [_payload(trend_up_ratio=0.1) for _ in range(60)]
    hist_facts = [_fact(_D5 + timedelta(days=i), payload=hist_payloads[i]) for i in range(60)]

    async def get(db, td, st, sk):
        if td == T:
            return _fact(T, scope_type=st, scope_key=sk, payload=_payload(trend_up_ratio=0.95))
        return None

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        if scope_key == "A" and to_date is not None:
            return hist_facts
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    result = await compute_scope_evidence(AsyncMock(), T, "concept", "A")

    hist = result["primitives"]["trend_up_ratio"]["historical"]
    assert hist["sample_count"] == 60
    # current (0.95) is NOT in the sample -> it is above all 0.1 samples -> max
    assert hist["percentile"] == 100.0


# ---------------------------------------------------------------------------
# N. same-family peer (current included) ; O. cross-family isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_same_family_includes_current(monkeypatch) -> None:
    prev = _prev_map()

    async def get(db, td, st, sk):
        return _fact(T, scope_type=st, scope_key=sk)

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        if scope_key is None:  # peer query (no scope_key filter)
            return [
                _fact(T, scope_type="concept", scope_key="A", payload=_payload(trend_up_ratio=0.31)),
                _fact(T, scope_type="concept", scope_key="B", payload=_payload(trend_up_ratio=0.5)),
                _fact(T, scope_type="concept", scope_key="C", payload=_payload(trend_up_ratio=0.7)),
            ]
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    result = await compute_scope_evidence(AsyncMock(), T, "concept", "A")

    peer = result["primitives"]["trend_up_ratio"]["peer"]
    assert peer["status"] == "ready"
    assert peer["peer_count"] == 3  # A (current) + B + C
    assert peer["percentile"] == pytest.approx(100.0 / 3)


@pytest.mark.asyncio
async def test_cross_family_isolation_no_other_family_peers(monkeypatch) -> None:
    prev = _prev_map()
    seen_scope_types: list[str] = []

    async def get(db, td, st, sk):
        return _fact(T, scope_type=st, scope_key=sk)

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        seen_scope_types.append(scope_type)
        if scope_key is None:
            # peer query must request ONLY concept (family isolation)
            return [_fact(T, scope_type=scope_type, scope_key="A")]
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    result = await compute_scope_evidence(AsyncMock(), T, "concept", "A")
    peer = result["primitives"]["trend_up_ratio"]["peer"]
    assert peer["peer_count"] == 1


# ---------------------------------------------------------------------------
# P. raw HHI peer disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_hhi_peer_disabled(monkeypatch) -> None:
    prev = _prev_map()

    async def get(db, td, st, sk):
        return _fact(T, scope_type=st, scope_key=sk)

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        if scope_key is None:
            return [_fact(T, scope_type="concept", scope_key="A")]
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    result = await compute_scope_evidence(AsyncMock(), T, "concept", "A")

    peer = result["primitives"]["price_raw_hhi"]["peer"]
    assert peer["status"] == "unavailable"
    assert peer["percentile"] is None
    assert peer["reason"] == RAW_HHI_PEER_DISABLED_REASON
    assert result["primitives"]["trend_up_ratio"]["peer"]["status"] == "ready"


# ---------------------------------------------------------------------------
# Q. no subjective keys ; R. input not mutated
# ---------------------------------------------------------------------------


def test_no_subjective_keys_in_output() -> None:
    banned = {
        "opportunity", "risk", "strong", "weak", "candidate", "filter",
        "discovery", "ranking", "score", "grade", "recommendation",
        "bullish", "bearish", "improving", "deteriorating",
    }
    ctx = {
        "current": build_current_context(0.5),
        "d1": build_delta_context(0.5, 0.4, T),
        "historical": build_historical_context(0.5, [0.1, 0.2, 0.3], date(2026, 1, 1), T),
        "peer": build_peer_context(0.5, [0.1, 0.2, 0.3]),
    }
    flattened = _flatten_keys(ctx)
    assert not (flattened & banned)


def test_input_payload_not_mutated() -> None:
    payload = _payload()
    original = copy.deepcopy(payload)
    for prim in PRIMITIVE_NAMES:
        extract_primitive(payload, prim)
    assert payload == original


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _flatten_keys(node) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            keys.add(str(k).lower())
            keys |= _flatten_keys(v)
    elif isinstance(node, list):
        for item in node:
            keys |= _flatten_keys(item)
    return keys
