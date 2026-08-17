"""Application integration tests — current-static Scope Dynamics path.

These tests prove ONLY the application composition responsibility (wiring +
source identity + provenance + date preservation).  No Duplicate Proof: the
frozen lower-layer math (Position percentile / EMA / Velocity / Acceleration /
Persistence / Phase thresholds / ObservationSeries registry extraction) is
already proven by its own durable test files.

T1. FORMAL CHAIN — the only mocked IO boundary is ``reconstruct_scope_series``
    (it returns real canonical L1 payloads); the application function really
    runs through ``build_observation_series`` + ``compute_scope_dynamics_analysis``.
T2. SOURCE PROVENANCE PRESERVED — membership mode / asof_date / member_count
    pass through unchanged.
T3. SOURCE CONTRACT GUARD — mode / scope identity / asof mismatch each raise
    ``CurrentStaticDynamicsSourceContractError`` before Phase.
T4. DATE VALIDATION — empty / duplicate / descending / future-vs-asof all raise
    ``ValueError`` and the source is never called.
T5. MEMBER MISSING DOES NOT AUTO-DOWNGRADE — a real ``compute_scope_observation``
    T with some members missing exact-T1 return but others valid -> EW scalar
    finite -> EW PrimitivePoint available=True (PRD §7.15.2).
T6. WHOLE SNAPSHOT GAP — a caller-axis date with no source row is preserved by
    the Builder as value=None available=False; Scope Dynamics output length
    still matches the full trading-date axis.
T7. NO PIT HISTORY PATH — a source spy proves the only source call is
    ``reconstruct_scope_series``.
T8. NO FUTURE LEAKAGE — appending future-but-<=asof dates/snapshots never
    changes the 0:T prefix Scope Dynamics output; the adapter never reorders or
    compresses dates.

No DB / network: the AsyncSession is a stand-in; the only IO boundary
(``reconstruct_scope_series``) is monkeypatched per test.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
)
from app.services import review_scope_dynamics_service as svc
from app.services.review_scope_dynamics_service import (
    CurrentStaticDynamicsSourceContractError,
)

pytestmark = pytest.mark.pure_unit

SCOPE_TYPE = "industry"
SCOPE_KEY = "electronics"


def _trading_days(start: date, count: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _member(mid: str, *, ret: float) -> MemberObservation:
    return MemberObservation(
        member_id=mid,
        price_candidate=True,
        return_1d=ret,
        amount=1_000_000.0,
        trend=Direction.UP,
        swing=Direction.UP,
        internal=Direction.UP,
        momentum=MomentumDirection.EXPANDING,
        regime_strength=0.5,
        vol_ratio20=1.0,
        vol_ratio200=2.0,
    )


def _missing_return_member(mid: str) -> MemberObservation:
    """A current-static member whose exact-T1 return is missing at T."""
    return MemberObservation(
        member_id=mid,
        price_candidate=True,
        return_1d=None,
        amount=1_000_000.0,
        trend=Direction.UP,
        swing=Direction.UP,
        internal=Direction.UP,
        momentum=MomentumDirection.EXPANDING,
        regime_strength=0.5,
        vol_ratio20=1.0,
        vol_ratio200=2.0,
    )


def _canonical_payload(
    trade_date: date,
    *,
    returns: Sequence[float],
    missing_count: int = 0,
) -> dict[str, Any]:
    """Real canonical L1 payload via the single owner (never hand-written)."""
    members = [_member(f"m{i}", ret=r) for i, r in enumerate(returns)]
    for i in range(missing_count):
        members.append(_missing_return_member(f"mx{len(returns) + i}"))
    return compute_scope_observation(
        scope_type=SCOPE_TYPE,
        scope_key=SCOPE_KEY,
        trade_date=trade_date,
        pit_member_ids=[m.member_id for m in members],
        members=members,
    )


def _make_source(
    days: Sequence[date],
    *,
    payload_builder: Callable[[date], dict[str, Any]],
    skip: set[int] | None = None,
    scope_override: dict[str, Any] | None = None,
    membership_override: dict[str, Any] | None = None,
    calls: list[dict[str, Any]] | None = None,
):
    """Fake ``reconstruct_scope_series`` returning real canonical L1 rows.

    Rows are filtered to the dates actually requested by the caller (mirrors the
    real source contract: one canonical row per requested historical T).
    """
    rows: list[dict[str, Any]] = []
    for i, d in enumerate(days):
        if skip is not None and i in skip:
            continue
        payload = payload_builder(d)
        rows.append(
            {
                "trade_date": d.isoformat(),
                "provided_member_count": payload["scope"]["provided_member_count"],
                "observation": payload,
            }
        )
    scope = (
        scope_override
        if scope_override is not None
        else {"scope_type": SCOPE_TYPE, "scope_key": SCOPE_KEY}
    )
    membership = (
        membership_override
        if membership_override is not None
        else {
            "mode": "current_static",
            # Empty axis is only used for invalid-input tests where the source is
            # never invoked; fall back to a fixed as-of so the fake can be built.
            "asof_date": (days[-1].isoformat() if days else date(1970, 1, 1).isoformat()),
            "member_count": 3,
        }
    )

    async def fake(
        db, scope_type: str, scope_key: str, trade_dates, *, asof_date: date
    ) -> dict[str, Any]:
        if calls is not None:
            calls.append(
                {
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                    "trade_dates": list(trade_dates),
                    "asof_date": asof_date,
                }
            )
        requested = {d if isinstance(d, date) else date.fromisoformat(str(d)) for d in trade_dates}
        filtered = [r for r in rows if date.fromisoformat(r["trade_date"]) in requested]
        return {"scope": scope, "membership": membership, "series": filtered}

    return fake


def _fixed_payloads(
    days: Sequence[date],
    *,
    returns_per_index: Callable[[int], float],
    missing_at: set[int] | None = None,
) -> Callable[[date], dict[str, Any]]:
    """Payload builder keyed by the date's index on the caller axis."""
    index = {d: i for i, d in enumerate(days)}

    def builder(d: date) -> dict[str, Any]:
        i = index[d]
        missing_count = 2 if missing_at is not None and i in missing_at else 0
        return _canonical_payload(
            d,
            returns=[returns_per_index(i)] * 3,
            missing_count=missing_count,
        )

    return builder


async def _run(days: Sequence[date], *, analysis_asof_date: date) -> dict[str, Any]:
    return await svc.compute_current_static_scope_dynamics(
        object(),
        SCOPE_TYPE,
        SCOPE_KEY,
        list(days),
        analysis_asof_date=analysis_asof_date,
    )


# ---------------------------------------------------------------------------
# T1. FORMAL CHAIN — real Builder + Scope Dynamics behind the only mocked source
# ---------------------------------------------------------------------------


def test_formal_chain_reaches_builder_and_scope_dynamics(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 130)
    payload_builder = _fixed_payloads(
        days, returns_per_index=lambda i: 0.02 * math.sin(i / 11) + 0.0015 * (i % 5)
    )
    monkeypatch.setattr(
        svc, "reconstruct_scope_series", _make_source(days, payload_builder=payload_builder)
    )

    out = asyncio.run(_run(days, analysis_asof_date=days[-1]))

    assert out["membership"]["mode"] == "current_static"
    assert out["scope"] == {"scope_type": SCOPE_TYPE, "scope_key": SCOPE_KEY}

    primitives = out["observation_series"]["primitives"]
    assert set(primitives) == {"equal_weight_return"}
    ew_points = primitives["equal_weight_return"]["points"]
    assert len(ew_points) == len(days) == 130

    position = out["scope_dynamics"]["historical_dynamics"]["position"]
    phase = out["scope_dynamics"]["dynamics_phase"]
    assert len(position) == len(phase) == len(days) == 130
    # The chain genuinely reaches Phase readiness on the long axis.
    assert sum(1 for p in phase if p["status"] == "ready") > 0
    assert sum(1 for p in ew_points if p["available"]) == len(days)


# ---------------------------------------------------------------------------
# T2. SOURCE PROVENANCE PRESERVED
# ---------------------------------------------------------------------------


def test_source_provenance_preserved(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 30)
    payload_builder = _fixed_payloads(days, returns_per_index=lambda i: 0.01)
    membership = {
        "mode": "current_static",
        "asof_date": days[-1].isoformat(),
        "member_count": 3,
    }
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series",
        _make_source(days, payload_builder=payload_builder, membership_override=membership),
    )

    out = asyncio.run(_run(days, analysis_asof_date=days[-1]))

    # Pass-through unchanged: no re-derivation, no rename, no overwrite.
    assert out["membership"] == membership
    assert out["scope"] == {"scope_type": SCOPE_TYPE, "scope_key": SCOPE_KEY}


# ---------------------------------------------------------------------------
# T3. SOURCE CONTRACT GUARD
# ---------------------------------------------------------------------------


def test_source_contract_guard_mode_mismatch(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 20)
    payload_builder = _fixed_payloads(days, returns_per_index=lambda i: 0.01)
    membership = {
        "mode": "historical_pit",
        "asof_date": days[-1].isoformat(),
        "member_count": 3,
    }
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series",
        _make_source(days, payload_builder=payload_builder, membership_override=membership),
    )

    with pytest.raises(CurrentStaticDynamicsSourceContractError):
        asyncio.run(_run(days, analysis_asof_date=days[-1]))


def test_source_contract_guard_scope_identity_mismatch(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 20)
    payload_builder = _fixed_payloads(days, returns_per_index=lambda i: 0.01)
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series",
        _make_source(
            days,
            payload_builder=payload_builder,
            scope_override={"scope_type": "industry_l1", "scope_key": SCOPE_KEY},
        ),
    )

    with pytest.raises(CurrentStaticDynamicsSourceContractError):
        asyncio.run(_run(days, analysis_asof_date=days[-1]))


def test_source_contract_guard_asof_mismatch(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 20)
    payload_builder = _fixed_payloads(days, returns_per_index=lambda i: 0.01)
    membership = {
        "mode": "current_static",
        "asof_date": date(2020, 1, 2).isoformat(),
        "member_count": 3,
    }
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series",
        _make_source(days, payload_builder=payload_builder, membership_override=membership),
    )

    with pytest.raises(CurrentStaticDynamicsSourceContractError):
        asyncio.run(_run(days, analysis_asof_date=days[-1]))


# ---------------------------------------------------------------------------
# T4. DATE VALIDATION — fail fast, source never called
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "days, asof",
    [
        ([], date(2026, 1, 5)),  # empty
        (
            [date(2026, 1, 5), date(2026, 1, 5)],
            date(2026, 1, 6),
        ),  # duplicate
        (
            [date(2026, 1, 6), date(2026, 1, 5)],
            date(2026, 1, 6),
        ),  # descending
        (
            [date(2026, 1, 5), date(2026, 1, 7)],
            date(2026, 1, 6),
        ),  # future vs asof
    ],
)
def test_date_validation_never_calls_source(monkeypatch, days: list[date], asof: date) -> None:
    calls: list[dict[str, Any]] = []
    # Real canonical payload builder: rows only materialize if the source is
    # actually invoked, which must never happen for an invalid caller axis.
    payload_builder = _fixed_payloads(days, returns_per_index=lambda i: 0.01)
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series",
        _make_source(days, payload_builder=payload_builder, calls=calls),
    )

    with pytest.raises(ValueError):
        asyncio.run(_run(days, analysis_asof_date=asof))

    assert calls == []  # source never called for invalid input


# ---------------------------------------------------------------------------
# T5. MEMBER MISSING DOES NOT AUTO-DOWNGRADE (PRD §7.15.2 at integration layer)
# ---------------------------------------------------------------------------


def test_member_missing_does_not_downgrade_primitive(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 3)
    payload_builder = _fixed_payloads(
        days,
        returns_per_index=lambda i: 0.01,
        missing_at={2},  # T3: 3 valid returns + 2 members missing exact-T1 return
    )
    monkeypatch.setattr(
        svc, "reconstruct_scope_series", _make_source(days, payload_builder=payload_builder)
    )

    out = asyncio.run(_run(days, analysis_asof_date=days[-1]))

    # Canonical EW scalar is finite despite 2 missing members.
    payload_t3 = payload_builder(days[2])
    assert payload_t3["price"]["equal_weight_return"] is not None

    points = out["observation_series"]["primitives"]["equal_weight_return"]["points"]
    assert len(points) == 3
    assert points[2]["available"] is True
    assert points[2]["value"] is not None
    assert points[2]["readiness"] == "ready"


# ---------------------------------------------------------------------------
# T6. WHOLE SNAPSHOT GAP — preserved as an unavailable slot, no fake payload
# ---------------------------------------------------------------------------


def test_whole_snapshot_gap_preserved(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 4)
    payload_builder = _fixed_payloads(days, returns_per_index=lambda i: 0.01)
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series",
        _make_source(days, payload_builder=payload_builder, skip={2}),  # T3 missing
    )

    out = asyncio.run(_run(days, analysis_asof_date=days[-1]))

    points = out["observation_series"]["primitives"]["equal_weight_return"]["points"]
    assert len(points) == 4  # slot preserved
    assert points[2]["trade_date"] == days[2].isoformat()
    assert points[2]["value"] is None
    assert points[2]["available"] is False
    assert points[2]["readiness"] == "unavailable"

    position = out["scope_dynamics"]["historical_dynamics"]["position"]
    phase = out["scope_dynamics"]["dynamics_phase"]
    assert len(position) == len(phase) == 4


# ---------------------------------------------------------------------------
# T7. NO PIT HISTORY PATH — source spy proves the single source call
# ---------------------------------------------------------------------------


def test_only_source_call_is_reconstruct_scope_series(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 30)
    payload_builder = _fixed_payloads(days, returns_per_index=lambda i: 0.01)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series",
        _make_source(days, payload_builder=payload_builder, calls=calls),
    )

    asyncio.run(_run(days, analysis_asof_date=days[-1]))

    assert len(calls) == 1
    assert calls[0]["scope_type"] == SCOPE_TYPE
    assert calls[0]["scope_key"] == SCOPE_KEY
    assert calls[0]["trade_dates"] == list(days)
    assert calls[0]["asof_date"] == days[-1]


# ---------------------------------------------------------------------------
# T8. NO FUTURE LEAKAGE — application adapter never reorders / compresses
# ---------------------------------------------------------------------------


def test_no_future_leakage_prefix_invariance(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 130)
    prefix = days[:100]
    payload_builder = _fixed_payloads(
        days, returns_per_index=lambda i: 0.02 * math.sin(i / 11) + 0.0015 * (i % 5)
    )
    source = _make_source(days, payload_builder=payload_builder)
    monkeypatch.setattr(svc, "reconstruct_scope_series", source)

    out_prefix = asyncio.run(_run(prefix, analysis_asof_date=days[-1]))
    out_full = asyncio.run(_run(days, analysis_asof_date=days[-1]))

    prefix_phase = out_prefix["scope_dynamics"]["dynamics_phase"]
    full_phase = out_full["scope_dynamics"]["dynamics_phase"]
    assert len(prefix_phase) == 100
    assert len(full_phase) == 130
    # Appending future-but-<=asof dates never changes the 0:100 prefix output.
    assert prefix_phase == full_phase[:100]
