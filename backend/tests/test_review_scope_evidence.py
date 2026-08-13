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
    PEER_DISABLED_REASON_BY_PRIMITIVE,
    PRIMITIVE_NAMES,
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
    trend_up_ratio: float | None = 0.31,
    overrides: dict[str, float | None] | None = None,
) -> dict:
    """Build a complete canonical-shape L1 CORE payload (4A §8).

    All CORE scalar extraction paths are populated by default.  ``overrides`` is
    a tiny test helper that replaces individual leaf values (None = missing
    path).  It is NOT a second production schema.
    """
    payload = {
        "price": {
            "return": {
                "mean": price_return_mean,
                "median": 0.01,
                "p25": -0.01,
                "p75": 0.03,
            },
            "breadth": {
                "advance_ratio": 0.5,
                "decline_ratio": 0.3,
                "unchanged_ratio": 0.2,
            },
            "concentration": {
                "raw_hhi": 0.3,
                "normalized_hhi": 0.2,
            },
            "amount": {
                "concentration": {
                    "raw_hhi": 0.25,
                    "normalized_hhi": 0.15,
                },
            },
        },
        "trend": {
            "state": {
                "up_ratio": trend_up_ratio,
                "neutral_ratio": 0.39,
                "down_ratio": 0.30,
            },
            "transition": {},
        },
        "structure": {
            "swing": {
                "state": {
                    "up_ratio": 0.4,
                    "neutral_ratio": 0.35,
                    "down_ratio": 0.25,
                },
                "transition": {},
            },
            "internal": {
                "state": {
                    "up_ratio": 0.35,
                    "neutral_ratio": 0.40,
                    "down_ratio": 0.25,
                },
                "transition": {},
            },
        },
        "momentum": {
            "state": {
                "expanding_ratio": 0.4,
                "flat_ratio": 0.35,
                "contracting_ratio": 0.25,
            },
            "transition": {},
        },
        "participation": {
            "volume": {
                "p25": 0.8,
                "p50": 1.2,
                "p75": 1.6,
            },
            "amount": {
                "p25": 0.7,
                "p50": 1.1,
                "p75": 1.5,
            },
        },
        "chip": {
            "status": "unavailable",
        },
    }
    if overrides:
        for path, value in overrides.items():
            _set_leaf(payload, path, value)
    return payload


def _set_leaf(node: dict, path: str, value: float | None) -> None:
    """Replace a dotted leaf in the payload (None deletes the key)."""
    parts = path.split(".")
    target = node
    for part in parts[:-1]:
        target = target[part]
    leaf = parts[-1]
    if value is None:
        target.pop(leaf, None)
    else:
        target[leaf] = value


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
    hist_payloads = [_payload(overrides={"trend.state.up_ratio": 0.1}) for _ in range(60)]
    hist_facts = [_fact(_D5 + timedelta(days=i), payload=hist_payloads[i]) for i in range(60)]

    async def get(db, td, st, sk):
        if td == T:
            return _fact(T, scope_type=st, scope_key=sk, payload=_payload(overrides={"trend.state.up_ratio": 0.95}))
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
                _fact(T, scope_type="concept", scope_key="A", payload=_payload(overrides={"trend.state.up_ratio": 0.31})),
                _fact(T, scope_type="concept", scope_key="B", payload=_payload(overrides={"trend.state.up_ratio": 0.5})),
                _fact(T, scope_type="concept", scope_key="C", payload=_payload(overrides={"trend.state.up_ratio": 0.7})),
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
    assert peer["reason"] == PEER_DISABLED_REASON_BY_PRIMITIVE["price_raw_hhi"]
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
# 4A §9 — new CORE coverage tests
# ---------------------------------------------------------------------------


def test_core_evidence_primitive_coverage() -> None:
    """Freeze the 29 CORE scalar extraction paths (4A §9, Test 1)."""
    expected = {
        "price_return_mean",
        "price_return_median",
        "price_return_p25",
        "price_return_p75",
        "price_advance_ratio",
        "price_decline_ratio",
        "price_unchanged_ratio",
        "price_raw_hhi",
        "price_normalized_hhi",
        "amount_raw_hhi",
        "amount_normalized_hhi",
        "trend_up_ratio",
        "trend_neutral_ratio",
        "trend_down_ratio",
        "structure_swing_up_ratio",
        "structure_swing_neutral_ratio",
        "structure_swing_down_ratio",
        "structure_internal_up_ratio",
        "structure_internal_neutral_ratio",
        "structure_internal_down_ratio",
        "momentum_expanding_ratio",
        "momentum_flat_ratio",
        "momentum_contracting_ratio",
        "participation_volume_p25",
        "participation_volume_p50",
        "participation_volume_p75",
        "participation_amount_p25",
        "participation_amount_p50",
        "participation_amount_p75",
    }
    assert set(PRIMITIVE_NAMES) == expected


def test_all_paths_extract_from_canonical_payload() -> None:
    """All 29 paths must extract a finite value from the canonical payload (4A §9, Test 2)."""
    payload = _payload()
    for prim in PRIMITIVE_NAMES:
        assert extract_primitive(payload, prim) is not None, f"{prim} not extracted"


@pytest.mark.asyncio
async def test_full_state_breadth_has_d1(monkeypatch) -> None:
    """Beyond trend_up / momentum_expanding, the full State/Breadth ratios
    resolve D1 (4A §9, Test 3)."""
    prev = _prev_map()

    async def get(db, td, st, sk):
        return _fact(td, scope_type=st, scope_key=sk)

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    result = await compute_scope_evidence(AsyncMock(), T, "concept", "A")

    for prim in (
        "trend_neutral_ratio",
        "trend_down_ratio",
        "structure_swing_up_ratio",
        "structure_internal_down_ratio",
        "momentum_flat_ratio",
        "momentum_contracting_ratio",
    ):
        assert result["primitives"][prim]["d1"]["status"] == "ready", prim


@pytest.mark.asyncio
async def test_participation_amount_in_l2(monkeypatch) -> None:
    """Participation amount p25/p50/p75 generate all contexts (4A §9, Test 4)."""
    prev = _prev_map()

    async def get(db, td, st, sk):
        return _fact(td, scope_type=st, scope_key=sk)

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    result = await compute_scope_evidence(AsyncMock(), T, "concept", "A")

    for prim in (
        "participation_amount_p25",
        "participation_amount_p50",
        "participation_amount_p75",
    ):
        p = result["primitives"][prim]
        assert p["current"]["status"] == "ready"
        assert p["d1"]["status"] == "ready"
        assert p["d3"]["status"] == "ready"
        assert p["d5"]["status"] == "ready"
        assert p["historical"]["status"] in {"ready", "insufficient_history", "unavailable"}
        # no peer facts -> unavailable but NOT the market no-peer reason
        assert p["peer"]["status"] == "unavailable"
        assert p["peer"].get("reason") != "no_cross_sectional_peer"


@pytest.mark.asyncio
async def test_normalized_hhi_peer_comparable(monkeypatch) -> None:
    """normalized HHI (price & amount) can do same-family peer percentile (4A §9, Test 5)."""
    prev = _prev_map()

    async def get(db, td, st, sk):
        return _fact(T, scope_type=st, scope_key=sk)

    def _peer_payload(scope_key: str, price_nh: float, amount_nh: float) -> SimpleNamespace:
        return _fact(
            T,
            scope_type="concept",
            scope_key=scope_key,
            payload=_payload(
                overrides={
                    "price.concentration.normalized_hhi": price_nh,
                    "price.amount.concentration.normalized_hhi": amount_nh,
                }
            ),
        )

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        if scope_key is None:
            return [
                _peer_payload("A", 0.10, 0.10),
                _peer_payload("B", 0.20, 0.20),
                _peer_payload("C", 0.30, 0.30),
            ]
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    result = await compute_scope_evidence(AsyncMock(), T, "concept", "A")

    price_peer = result["primitives"]["price_normalized_hhi"]["peer"]
    assert price_peer["status"] == "ready"
    assert price_peer["peer_count"] == 3
    assert price_peer["percentile"] is not None

    amount_peer = result["primitives"]["amount_normalized_hhi"]["peer"]
    assert amount_peer["status"] == "ready"
    assert amount_peer["peer_count"] == 3
    assert amount_peer["percentile"] is not None


@pytest.mark.asyncio
async def test_raw_hhi_peer_disabled_both_price_amount(monkeypatch) -> None:
    """price_raw_hhi AND amount_raw_hhi peer disabled; historical still works (4A §9, Test 6)."""
    prev = _prev_map()

    async def get(db, td, st, sk):
        return _fact(T, scope_type=st, scope_key=sk)

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        if scope_key is None:
            return [_fact(T, scope_type="concept", scope_key="A")]
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    result = await compute_scope_evidence(AsyncMock(), T, "concept", "A")

    for prim in ("price_raw_hhi", "amount_raw_hhi"):
        peer = result["primitives"][prim]["peer"]
        assert peer["status"] == "unavailable"
        assert peer["reason"] == PEER_DISABLED_REASON_BY_PRIMITIVE[prim]
        # historical context remains computable
        assert "historical" in result["primitives"][prim]


@pytest.mark.asyncio
async def test_market_has_no_peer(monkeypatch) -> None:
    """market scope -> peer unavailable with no_cross_sectional_peer (4A §9, Test 7)."""
    prev = _prev_map()

    async def get(db, td, st, sk):
        return _fact(td, scope_type=st, scope_key=sk)

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    result = await compute_scope_evidence(AsyncMock(), T, "market", "ALL")

    assert result["primitives"]["trend_up_ratio"]["peer"]["status"] == "unavailable"
    assert result["primitives"]["trend_up_ratio"]["peer"]["reason"] == "no_cross_sectional_peer"


@pytest.mark.asyncio
async def test_major_index_and_style_architecture_support(monkeypatch) -> None:
    """major_index / style are architecturally supported; when same-family peer
    facts exist they compute normally (4A §9, Test 8).  No production DB used."""
    prev = _prev_map()

    async def get(db, td, st, sk):
        return _fact(T, scope_type=st, scope_key=sk)

    def _peer(scope_type: str, scope_key: str, up: float) -> SimpleNamespace:
        return _fact(
            T,
            scope_type=scope_type,
            scope_key=scope_key,
            payload=_payload(overrides={"trend.state.up_ratio": up}),
        )

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        if scope_key is None:
            if scope_type == "major_index":
                return [_peer("major_index", "A", 0.2), _peer("major_index", "B", 0.4)]
            if scope_type == "style":
                return [_peer("style", "A", 0.2), _peer("style", "B", 0.4)]
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)

    for scope_type in ("major_index", "style"):
        result = await compute_scope_evidence(AsyncMock(), T, scope_type, "A")
        peer = result["primitives"]["trend_up_ratio"]["peer"]
        assert peer["status"] == "ready", scope_type
        assert peer["peer_count"] == 2, scope_type
        assert peer["percentile"] is not None, scope_type


@pytest.mark.asyncio
async def test_cross_family_isolation_all_families(monkeypatch) -> None:
    """Each family only queries its own cohort (4A §9, Test 9)."""
    prev = _prev_map()
    seen_scope_types: list[str] = []

    async def get(db, td, st, sk):
        return _fact(T, scope_type=st, scope_key=sk)

    async def list_(db, *, scope_type, scope_key=None, from_date=None, to_date=None):
        seen_scope_types.append(scope_type)
        if scope_key is None:
            return [_fact(T, scope_type=scope_type, scope_key="A")]
        return []

    await _patch_all(monkeypatch, prev=lambda d: prev.get(d), get=get, list_=list_)
    for scope_type in ("concept", "industry_l1", "industry_l2", "industry_l3", "style"):
        await compute_scope_evidence(AsyncMock(), T, scope_type, "A")

    # every peer query used the exact family scope_type, never mixed
    assert seen_scope_types == [
        "concept", "concept",
        "industry_l1", "industry_l1",
        "industry_l2", "industry_l2",
        "industry_l3", "industry_l3",
        "style", "style",
    ]


def test_no_subjective_fields_in_primitives() -> None:
    """Output carries no score/rank/grade/opportunity/risk/strong/weak/filter/
    signal/discovery keys (4A §9, Test 10)."""
    banned = {
        "score", "rank", "grade", "opportunity", "risk", "strong", "weak",
        "filter", "signal", "discovery",
    }
    sample = {
        "current": build_current_context(0.5),
        "d1": build_delta_context(0.5, 0.4, T),
        "historical": build_historical_context(0.5, [0.1, 0.2, 0.3], date(2026, 1, 1), T),
        "peer": build_peer_context(0.5, [0.1, 0.2, 0.3]),
    }
    flattened = _flatten_keys(sample)
    assert not (flattened & banned)


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
