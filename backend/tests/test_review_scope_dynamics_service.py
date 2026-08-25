"""Application integration tests — current-static Scope Dynamics path.

These tests prove ONLY the application composition responsibility (wiring +
source identity + provenance + date preservation).  No Duplicate Proof: the
frozen lower-layer math (Position percentile / EMA / Velocity / Acceleration /
Persistence / Phase thresholds / ObservationSeries registry extraction) is
already proven by its own durable test files.

[REVIEW-EXECUTION-PATH-CONSOLIDATION] 应用组合已收口为唯一 batch owner
``compute_current_static_scope_dynamics_batch``（单 scope 也走同一 owner，
batch size = 1）；单 scope 入口 ``compute_current_static_scope_dynamics`` 与
其底层 mock 边界 ``reconstruct_scope_series`` 已删除。本文件所有断言均针对 batch
owner，且唯一的 IO mock 边界是 ``reconstruct_scope_series_batch``。

T1. FORMAL CHAIN — the only mocked IO boundary is ``reconstruct_scope_series_batch``
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
    ``reconstruct_scope_series_batch``.
T8. NO FUTURE LEAKAGE — appending future-but-<=asof dates/snapshots never
    changes the 0:T prefix Scope Dynamics output; the adapter never reorders or
    compresses dates.

No DB / network: the AsyncSession is a stand-in; the only IO boundary
(``reconstruct_scope_series_batch``) is monkeypatched per test.
"""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.scope_observation import (
    MemberObservation,
    _return_distribution,
    compute_scope_observation,
)
from app.services import review_scope_dynamics_service as svc
from app.services.review_historical_ew_db_service import (
    HistoricalEWBatchResult,
    HistoricalEWScopeResult,
    HistoricalEWSourceMetrics,
)
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
        event_coverage_member_ids=None,
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
    """Fake ``reconstruct_scope_series_batch`` (single-scope) returning real rows.

    The single composition owner is the batch entry point; a batch of size one
    routes through the SAME ``reconstruct_scope_series_batch`` IO boundary.  Rows
    are filtered to the dates actually requested by the caller (mirrors the real
    source contract: one canonical row per requested historical T).
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
        db, scope_type: str, scope_keys, trade_dates, *, asof_date: date,
        union_member_cap: int = 4096,
    ) -> list[dict[str, Any]]:
        if calls is not None:
            calls.append(
                {
                    "scope_type": scope_type,
                    "scope_keys": list(scope_keys),
                    "trade_dates": list(trade_dates),
                    "asof_date": asof_date,
                }
            )
        requested = {d if isinstance(d, date) else date.fromisoformat(str(d)) for d in trade_dates}
        filtered = [r for r in rows if date.fromisoformat(r["trade_date"]) in requested]
        return [
            {"scope": scope, "membership": membership, "series": filtered}
            for _ in scope_keys
        ]

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
    out = await svc.compute_current_static_scope_dynamics_batch(
        object(),
        SCOPE_TYPE,
        [SCOPE_KEY],
        list(days),
        analysis_asof_date=analysis_asof_date,
    )
    # Single-scope composition routes through the SAME batch owner (batch size 1).
    return out[0]


# ---------------------------------------------------------------------------
# T1. FORMAL CHAIN — real Builder + Scope Dynamics behind the only mocked source
# ---------------------------------------------------------------------------


def test_formal_chain_reaches_builder_and_scope_dynamics(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 130)
    payload_builder = _fixed_payloads(
        days, returns_per_index=lambda i: 0.02 * math.sin(i / 11) + 0.0015 * (i % 5)
    )
    monkeypatch.setattr(
        svc, "reconstruct_scope_series_batch", _make_source(days, payload_builder=payload_builder)
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
        "reconstruct_scope_series_batch",
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
        "reconstruct_scope_series_batch",
        _make_source(days, payload_builder=payload_builder, membership_override=membership),
    )

    with pytest.raises(CurrentStaticDynamicsSourceContractError):
        asyncio.run(_run(days, analysis_asof_date=days[-1]))


def test_source_contract_guard_scope_identity_mismatch(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 20)
    payload_builder = _fixed_payloads(days, returns_per_index=lambda i: 0.01)
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series_batch",
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
        "reconstruct_scope_series_batch",
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
        "reconstruct_scope_series_batch",
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
        svc, "reconstruct_scope_series_batch", _make_source(days, payload_builder=payload_builder)
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
        "reconstruct_scope_series_batch",
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


def test_only_source_call_is_reconstruct_scope_series_batch(monkeypatch) -> None:
    days = _trading_days(date(2026, 1, 5), 30)
    payload_builder = _fixed_payloads(days, returns_per_index=lambda i: 0.01)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series_batch",
        _make_source(days, payload_builder=payload_builder, calls=calls),
    )

    asyncio.run(_run(days, analysis_asof_date=days[-1]))

    assert len(calls) == 1
    assert calls[0]["scope_type"] == SCOPE_TYPE
    assert calls[0]["scope_keys"] == [SCOPE_KEY]
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
    monkeypatch.setattr(svc, "reconstruct_scope_series_batch", source)

    out_prefix = asyncio.run(_run(prefix, analysis_asof_date=days[-1]))
    out_full = asyncio.run(_run(days, analysis_asof_date=days[-1]))

    prefix_phase = out_prefix["scope_dynamics"]["dynamics_phase"]
    full_phase = out_full["scope_dynamics"]["dynamics_phase"]
    assert len(prefix_phase) == 100
    assert len(full_phase) == 130
    # Appending future-but-<=asof dates never changes the 0:100 prefix output.
    assert prefix_phase == full_phase[:100]


# ---------------------------------------------------------------------------
# T9–T13. VEC-1B — BATCH composition is THE single composition owner
# ---------------------------------------------------------------------------
# The ONLY mocked IO boundary is ``reconstruct_scope_series_batch``; each
# returned entry is then composed by the SAME ``_compose_scope_dynamics_from_reconstruction``
# helper (there is no separate single-scope path).  These tests prove: batch
# output shape / identity, single source call, empty batch fast-path, and batch
# contract guard.


def _make_batch_source(
    days: Sequence[date],
    *,
    payload_builder: Callable[[date, str], dict[str, Any]],
    scope_keys: Sequence[str],
    skip: set[int] | None = None,
    membership_override: dict[str, Any] | None = None,
    calls: list[dict[str, Any]] | None = None,
):
    """Fake ``reconstruct_scope_series_batch`` returning real canonical L1 rows.

    Mirrors the real batch source: one entry per scope_key, rows filtered to
    the requested trading dates, membership provenance shared.
    """
    rows_by_scope: dict[str, list[dict[str, Any]]] = {}
    for sk in scope_keys:
        rows: list[dict[str, Any]] = []
        for i, d in enumerate(days):
            if skip is not None and i in skip:
                continue
            payload = payload_builder(d, sk)
            rows.append(
                {
                    "trade_date": d.isoformat(),
                    "provided_member_count": payload["scope"]["provided_member_count"],
                    "observation": payload,
                }
            )
        rows_by_scope[sk] = rows

    def _membership(asof: date) -> dict[str, Any]:
        if membership_override is not None:
            return membership_override
        return {
            "mode": "current_static",
            "asof_date": asof.isoformat(),
            "member_count": 3,
        }

    async def fake(
        db,
        scope_type: str,
        scope_keys_arg,
        trade_dates,
        *,
        asof_date: date,
        union_member_cap: int = 4096,
    ) -> list[dict[str, Any]]:
        if calls is not None:
            calls.append(
                {
                    "scope_type": scope_type,
                    "scope_keys": list(scope_keys_arg),
                    "trade_dates": list(trade_dates),
                    "asof_date": asof_date,
                    "union_member_cap": union_member_cap,
                }
            )
        requested = {d if isinstance(d, date) else date.fromisoformat(str(d)) for d in trade_dates}
        out: list[dict[str, Any]] = []
        for sk in scope_keys_arg:
            filtered = [
                r for r in rows_by_scope[sk]
                if date.fromisoformat(r["trade_date"]) in requested
            ]
            out.append(
                {
                    "scope": {"scope_type": scope_type, "scope_key": sk},
                    "membership": _membership(asof_date),
                    "series": filtered,
                }
            )
        return out

    return fake


def test_batch_composes_all_scopes_in_order(monkeypatch) -> None:
    """T9. Batch output is one result per scope_key (input order), each reaching
    the full Builder + Scope Dynamics chain."""
    days = _trading_days(date(2026, 1, 5), 130)
    scope_keys = ["alpha", "beta"]

    def builder(d: date, sk: str) -> dict[str, Any]:
        # Distinct returns per scope so identity is meaningful.
        return _canonical_payload(
            d, returns=[0.01 * (1 if sk == "alpha" else -1) + 0.001 * (i % 3) for i in (0, 1, 2)]
        )

    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series_batch",
        _make_batch_source(days, payload_builder=builder, scope_keys=scope_keys),
    )

    out = asyncio.run(
        svc.compute_current_static_scope_dynamics_batch(
            object(), SCOPE_TYPE, scope_keys, list(days), analysis_asof_date=days[-1]
        )
    )

    assert [r["scope"]["scope_key"] for r in out] == scope_keys
    for result, _sk in zip(out, scope_keys, strict=True):
        assert result["membership"]["mode"] == "current_static"
        points = result["observation_series"]["primitives"]["equal_weight_return"]["points"]
        phase = result["scope_dynamics"]["dynamics_phase"]
        assert len(points) == len(phase) == len(days)


def test_batch_calls_batch_source_once(monkeypatch) -> None:
    """T11. The batch path invokes ``reconstruct_scope_series_batch`` exactly once
    (no per-scope re-entry), sharing the member window across scopes."""
    days = _trading_days(date(2026, 1, 5), 30)
    scope_keys = ["alpha", "beta"]

    def builder(d: date, sk: str) -> dict[str, Any]:
        return _canonical_payload(d, returns=[0.01, 0.01, 0.01])

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series_batch",
        _make_batch_source(
            days, payload_builder=builder, scope_keys=scope_keys, calls=calls
        ),
    )

    asyncio.run(
        svc.compute_current_static_scope_dynamics_batch(
            object(), SCOPE_TYPE, scope_keys, list(days), analysis_asof_date=days[-1]
        )
    )

    assert len(calls) == 1
    assert calls[0]["scope_keys"] == list(scope_keys)
    assert calls[0]["trade_dates"] == list(days)
    assert calls[0]["asof_date"] == days[-1]


def test_batch_empty_scope_keys_returns_empty(monkeypatch) -> None:
    """T12. Empty scope_keys returns [] without touching the source."""
    days = _trading_days(date(2026, 1, 5), 30)
    calls: list[dict[str, Any]] = []

    def builder(d: date, sk: str) -> dict[str, Any]:
        return _canonical_payload(d, returns=[0.01, 0.01, 0.01])

    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series_batch",
        _make_batch_source(days, payload_builder=builder, scope_keys=[], calls=calls),
    )

    out = asyncio.run(
        svc.compute_current_static_scope_dynamics_batch(
            object(), SCOPE_TYPE, [], list(days), analysis_asof_date=days[-1]
        )
    )
    assert out == []
    assert calls == []


def test_batch_contract_guard_rejects_non_current_static(monkeypatch) -> None:
    """T13. A batch entry whose membership mode is not current_static fails fast
    with ``CurrentStaticDynamicsSourceContractError`` before composition."""
    days = _trading_days(date(2026, 1, 5), 20)
    scope_keys = ["alpha"]

    def builder(d: date, sk: str) -> dict[str, Any]:
        return _canonical_payload(d, returns=[0.01, 0.01, 0.01])

    membership = {
        "mode": "historical_pit",
        "asof_date": days[-1].isoformat(),
        "member_count": 3,
    }
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series_batch",
        _make_batch_source(
            days,
            payload_builder=builder,
            scope_keys=scope_keys,
            membership_override=membership,
        ),
    )

    with pytest.raises(CurrentStaticDynamicsSourceContractError):
        asyncio.run(
            svc.compute_current_static_scope_dynamics_batch(
                object(), SCOPE_TYPE, scope_keys, list(days), analysis_asof_date=days[-1]
            )
        )


# ---------------------------------------------------------------------------
# M5-D1. columnar_ew source — integration only, production default unchanged
# ---------------------------------------------------------------------------
# A second internal historical source is wired into the SAME batch owner behind
# the ``historical_source`` selector (default keeps the legacy
# ``"reconstruction"`` owner, so production behaviour is unchanged).  The ONLY
# added IO boundary is ``compute_current_static_historical_ew_batch`` (the real
# close-only SQL adapter, mocked here); ``build_scope_dynamics_from_ew`` runs the
# REAL canonical ObservationSeries Builder + Scope Dynamics analysis, i.e. the
# recorded EW values flow through the true Dyn chain (no fake math).
#
# Coverage per M5-D1 acceptance:
#   D1-1 old mode output unchanged            -> ``test_d1_default_owner_is_reconstruction``
#   D1-2 new mode exact output                -> ``test_d1_new_mode_exact_output``
#   D1-3 same scope order                     -> ``test_d1_batch_scope_order_preserved``
#   D1-4/5 same observation_series / dynamics shape -> ``test_d1_columnar_parity_with_reconstruction``
#   D1-6 missing EW stays unavailable         -> ``test_d1_missing_ew_unavailable_not_zero``
#   D1-7 invalid calendar/member source fail closed -> ``test_d1_source_violations_fail_closed``
#   D1-8 no ndarray escapes public result     -> ``test_d1_no_ndarray_escapes_public_result``
#   D1-9 no DB writes                         -> ``test_d1_no_db_writes_only_ew_adapter_called``
#   D1-10 old reconstruction still reachable  -> ``test_d1_old_reconstruction_still_reachable``


def _ew_scope_result(
    sk: str,
    *,
    days: Sequence[date],
    ew_values: Sequence[float | None],
    scope_name: str = "",
    member_count: int = 3,
) -> HistoricalEWScopeResult:
    return HistoricalEWScopeResult(
        scope_key=sk,
        scope_name=scope_name or sk,
        member_count=member_count,
        ew_values=tuple(ew_values),
    )


def _make_ew_source(
    days: Sequence[date],
    *,
    ew_by_scope: dict[str, Sequence[float | None]],
    scope_names: dict[str, str] | None = None,
    member_counts: dict[str, int] | None = None,
    calls: list[dict[str, Any]] | None = None,
    order: Sequence[str] | None = None,
):
    """Fake ``compute_current_static_historical_ew_batch`` (the C1/C2 SQL adapter).

    Returns a real ``HistoricalEWBatchResult`` with per-scope EW series aligned
    to the requested axis.  ``order`` lets a test inject a scope iteration that
    differs from the caller's order to prove the fail-closed ordering guard.
    ``ew_values`` length is used as-is, so a test can pass a wrong-length series
    to prove the adapter-length guard.
    """

    async def fake(
        db, scope_type: str, scope_keys, trade_dates, *, analysis_asof_date: date,
    ) -> HistoricalEWBatchResult:
        if calls is not None:
            calls.append(
                {
                    "scope_type": scope_type,
                    "scope_keys": list(scope_keys),
                    "trade_dates": list(trade_dates),
                    "analysis_asof_date": analysis_asof_date,
                }
            )
        if len(list(trade_dates)) != len(days):
            raise AssertionError(
                f"test harness: axis length {len(list(trade_dates))} != {len(days)}"
            )
        keys = list(scope_keys) if order is None else list(order)
        scopes = [
            _ew_scope_result(
                k,
                days=days,
                ew_values=ew_by_scope[k],
                scope_name=(scope_names or {}).get(k, ""),
                member_count=(member_counts or {}).get(k, 3),
            )
            for k in keys
        ]
        return HistoricalEWBatchResult(
            scope_type=scope_type,
            analysis_asof_date=analysis_asof_date,
            trade_dates=tuple(trade_dates),
            scopes=tuple(scopes),
            metrics=HistoricalEWSourceMetrics(),
        )

    return fake


async def _run_ew(
    scope_keys: Sequence[str],
    days: Sequence[date],
    *,
    historical_source: str = "columnar_ew",
    analysis_asof_date: date | None = None,
) -> list[dict[str, Any]]:
    return await svc.compute_current_static_scope_dynamics_batch(
        object(),
        SCOPE_TYPE,
        list(scope_keys),
        list(days),
        analysis_asof_date=analysis_asof_date or days[-1],
        historical_source=historical_source,
    )


def test_d1_default_owner_is_reconstruction() -> None:
    """D1-1. The selector defaults to the legacy owner; production behaviour is
    unchanged — the new source is reachable ONLY via explicit opt-in."""
    sig = inspect.signature(svc.compute_current_static_scope_dynamics_batch)
    assert sig.parameters["historical_source"].default == "reconstruction"


def test_d1_new_mode_exact_output(monkeypatch) -> None:
    """D1-2. columnar_ew routes the recorded EW values through the REAL canonical
    chain: every finite EW becomes an available PrimitivePoint with the exact
    same value (no coercion), and the canonical Dynamics arrays match the axis."""
    days = _trading_days(date(2026, 1, 5), 4)
    ew = [0.01, 0.02, 0.03, 0.04]
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        svc,
        "compute_current_static_historical_ew_batch",
        _make_ew_source(days, ew_by_scope={SCOPE_KEY: ew}, calls=calls),
    )

    out = asyncio.run(_run_ew([SCOPE_KEY], days))[0]

    assert out["scope"] == {"scope_type": SCOPE_TYPE, "scope_key": SCOPE_KEY}
    assert out["membership"]["mode"] == "current_static"
    assert out["membership"]["asof_date"] == days[-1].isoformat()
    assert out["membership"]["member_count"] == 3
    assert out["metrics"]["historical_source"] == "columnar_ew"
    assert out["metrics"]["batch_reconstruction_ms"] == 0.0
    assert "batch_ew_source_ms" in out["metrics"]

    primitives = out["observation_series"]["primitives"]
    assert set(primitives) == {"equal_weight_return"}
    points = primitives["equal_weight_return"]["points"]
    assert len(points) == 4
    for point, expected in zip(points, ew, strict=True):
        assert point["available"] is True
        assert point["value"] == expected
        assert point["readiness"] == "ready"

    position = out["scope_dynamics"]["historical_dynamics"]["position"]
    phase = out["scope_dynamics"]["dynamics_phase"]
    assert len(position) == len(phase) == 4

    # The only mocked IO boundary is the EW adapter: exactly one call, with the
    # caller-supplied axis passed through untouched.
    assert len(calls) == 1
    assert calls[0]["scope_keys"] == [SCOPE_KEY]
    assert calls[0]["trade_dates"] == list(days)
    assert calls[0]["analysis_asof_date"] == days[-1]


def test_d1_batch_scope_order_preserved(monkeypatch) -> None:
    """D1-3. Multi-scope columnar_ew returns one result per scope_key in the
    caller's order, each scope carrying its own EW series (no cross-wiring)."""
    days = _trading_days(date(2026, 1, 5), 5)
    scope_keys = ["alpha", "beta"]
    ew_by_scope = {
        "alpha": [0.01, 0.02, 0.03, 0.04, 0.05],
        "beta": [0.5, 0.4, 0.3, 0.2, 0.1],
    }
    monkeypatch.setattr(
        svc,
        "compute_current_static_historical_ew_batch",
        _make_ew_source(days, ew_by_scope=ew_by_scope),
    )

    out = asyncio.run(_run_ew(scope_keys, days))

    assert [r["scope"]["scope_key"] for r in out] == scope_keys
    for result, sk in zip(out, scope_keys, strict=True):
        points = result["observation_series"]["primitives"]["equal_weight_return"]["points"]
        assert [p["value"] for p in points] == ew_by_scope[sk]
        assert len(result["scope_dynamics"]["dynamics_phase"]) == len(days)
        assert result["membership"]["member_count"] == 3


def test_d1_columnar_parity_with_reconstruction(monkeypatch) -> None:
    """D1-4/D1-5. Feeding the SAME EW facts through both owners yields identical
    observation_series points AND identical canonical Dynamics — every value and
    shape field matches exactly (shared canonical reducer, PRD red line)."""
    days = _trading_days(date(2026, 1, 5), 20)
    scope_keys = ["alpha", "beta"]
    index = {d: i for i, d in enumerate(days)}
    missing_day_index = 2

    def builder(d: date, sk: str) -> dict[str, Any]:
        if index[d] == missing_day_index:
            # Whole-scope missing EW on both owners: all members lack the
            # exact-T1 return -> EW None, snapshot still present (readiness
            # "ready"), so both point "readiness" fields must match too.
            return _canonical_payload(d, returns=[], missing_count=3)
        return _canonical_payload(d, returns=[0.01, 0.01, -0.005])

    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series_batch",
        _make_batch_source(days, payload_builder=builder, scope_keys=scope_keys),
    )
    old_out = asyncio.run(
        svc.compute_current_static_scope_dynamics_batch(
            object(), SCOPE_TYPE, scope_keys, list(days), analysis_asof_date=days[-1]
        )
    )

    # The columnar side must reproduce the same EW facts.  EW is the canonical
    # sorted-mean of the member returns, so derive the recorded EW via the SAME
    # frozen reducer instead of hand-rolling floats.
    recon_ew = _return_distribution([0.01, 0.01, -0.005])["mean"]
    ew_series = [
        None if i == missing_day_index else recon_ew for i in range(len(days))
    ]
    ew_by_scope = {sk: list(ew_series) for sk in scope_keys}
    monkeypatch.setattr(
        svc,
        "compute_current_static_historical_ew_batch",
        _make_ew_source(days, ew_by_scope=ew_by_scope),
    )
    new_out = asyncio.run(_run_ew(scope_keys, days))

    assert [r["scope"]["scope_key"] for r in new_out] == scope_keys

    def _obs_signature(result: dict[str, Any]) -> dict[str, Any]:
        return {
            "obs_keys": sorted(result["observation_series"].keys()),
            "primitive_keys": sorted(result["observation_series"]["primitives"].keys()),
            "point_keys": sorted(
                result["observation_series"]["primitives"]["equal_weight_return"]["points"][0]
            ),
            "dyn_keys": sorted(result["scope_dynamics"].keys()),
            "hd_keys": sorted(result["scope_dynamics"]["historical_dynamics"].keys()),
            "phase_keys": sorted(result["scope_dynamics"]["dynamics_phase"][0]),
        }

    for old_r, new_r in zip(old_out, new_out, strict=True):
        # Same contract shape on the non-metrics sections.
        assert _obs_signature(old_r) == _obs_signature(new_r)
        # Same primitive values (bit-exact — identical canonical reducer).
        old_points = old_r["observation_series"]["primitives"]["equal_weight_return"]["points"]
        new_points = new_r["observation_series"]["primitives"]["equal_weight_return"]["points"]
        assert new_points == old_points
        # Same canonical Dynamics output (bit-exact).
        assert new_r["scope_dynamics"] == old_r["scope_dynamics"]


def test_d1_missing_ew_unavailable_not_zero(monkeypatch) -> None:
    """D1-6. A None EW slot stays unavailable (never coerced to 0.0): the
    Builder renders an unavailable point and the Dynamics axis keeps its shape."""
    days = _trading_days(date(2026, 1, 5), 4)
    ew = [0.01, None, 0.03, None]
    monkeypatch.setattr(
        svc,
        "compute_current_static_historical_ew_batch",
        _make_ew_source(days, ew_by_scope={SCOPE_KEY: ew}),
    )

    out = asyncio.run(_run_ew([SCOPE_KEY], days))[0]

    points = out["observation_series"]["primitives"]["equal_weight_return"]["points"]
    assert [p["value"] for p in points] == [0.01, None, 0.03, None]
    assert [p["available"] for p in points] == [True, False, True, False]
    # A date whose EW scalar is not available is a PRESENT snapshot with a
    # missing value (both owners), so readiness stays "ready" — unlike a whole
    # snapshot gap which the Builder registers as "unavailable".  The core
    # requirement is value remains None / unavailable — never coerced to 0.0.
    assert points[1]["value"] is None
    assert points[1]["readiness"] == "ready"
    position = out["scope_dynamics"]["historical_dynamics"]["position"]
    phase = out["scope_dynamics"]["dynamics_phase"]
    assert len(position) == len(phase) == 4


def test_d1_source_violations_fail_closed(monkeypatch) -> None:
    """D1-7. Adapter / calendar violations fail closed — no silent coercion:
    scope ordering mismatch -> RuntimeError; EW series length mismatch ->
    ValueError; unknown selector -> ValueError before ANY source call."""
    days = _trading_days(date(2026, 1, 5), 4)
    scope_keys = ["alpha", "beta"]
    ew_by_scope = {"alpha": [0.01, 0.02, 0.03, 0.04], "beta": [0.05, 0.06, 0.07, 0.08]}

    # (a) adapter iterates scopes in a different order than the caller asked
    calls_a: list[dict[str, Any]] = []
    monkeypatch.setattr(
        svc,
        "compute_current_static_historical_ew_batch",
        _make_ew_source(
            days,
            ew_by_scope=ew_by_scope,
            calls=calls_a,
            order=["beta", "alpha"],
        ),
    )
    with pytest.raises(RuntimeError, match="ordering mismatch"):
        asyncio.run(_run_ew(scope_keys, days))
    assert len(calls_a) == 1

    # (b) adapter returned an EW series of the wrong axis length
    monkeypatch.setattr(
        svc,
        "compute_current_static_historical_ew_batch",
        _make_ew_source(
            days,
            ew_by_scope={"alpha": [0.01, 0.02], "beta": [0.05, 0.06, 0.07, 0.08]},
        ),
    )
    with pytest.raises(ValueError, match="length"):
        asyncio.run(_run_ew(scope_keys, days))

    # (c) unknown selector: fails before trade-date validation / any source
    calls_c: list[dict[str, Any]] = []
    monkeypatch.setattr(
        svc,
        "compute_current_static_historical_ew_batch",
        _make_ew_source(days, ew_by_scope={SCOPE_KEY: [0.01] * 4}, calls=calls_c),
    )
    with pytest.raises(ValueError, match="historical_source"):
        asyncio.run(
            svc.compute_current_static_scope_dynamics_batch(
                object(), SCOPE_TYPE, [SCOPE_KEY], list(days),
                analysis_asof_date=days[-1],
                historical_source="matrix",
            )
        )
    assert calls_c == []


def test_d1_no_ndarray_escapes_public_result(monkeypatch) -> None:
    """D1-8. The public result is plain Python data — no numpy scalars / arrays
    leak out of the columnar source into the orchestrator-facing contract."""

    def _assert_no_numpy(value: Any, path: str = "result") -> None:
        if isinstance(value, (np.ndarray, np.floating, np.integer, np.bool_)):
            raise AssertionError(f"numpy type escaped at {path}: {type(value).__name__}")
        if isinstance(value, dict):
            for k, v in value.items():
                _assert_no_numpy(v, f"{path}.{k}")
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                _assert_no_numpy(v, f"{path}[{i}]")

    days = _trading_days(date(2026, 1, 5), 10)
    ew = [0.01 * (i % 4) for i in range(len(days))]
    monkeypatch.setattr(
        svc,
        "compute_current_static_historical_ew_batch",
        _make_ew_source(days, ew_by_scope={SCOPE_KEY: ew}),
    )

    out = asyncio.run(_run_ew([SCOPE_KEY], days))
    for result in out:
        _assert_no_numpy(result)


def test_d1_no_db_writes_only_ew_adapter_called(monkeypatch) -> None:
    """D1-9. The composition path performs NO session writes of its own: the
    only IO boundary touched is the (mocked) close-only SQL adapter, called
    exactly once; the legacy reconstruction source is never invoked."""
    days = _trading_days(date(2026, 1, 5), 4)
    ew = [0.01, 0.02, 0.03, 0.04]
    calls: list[dict[str, Any]] = []

    def _recon_must_not_run(*args, **kwargs):
        raise AssertionError("reconstruct_scope_series_batch must not run for columnar_ew")

    monkeypatch.setattr(
        svc,
        "compute_current_static_historical_ew_batch",
        _make_ew_source(days, ew_by_scope={SCOPE_KEY: ew}, calls=calls),
    )
    monkeypatch.setattr(svc, "reconstruct_scope_series_batch", _recon_must_not_run)

    out = asyncio.run(_run_ew([SCOPE_KEY], days))
    assert len(out) == 1
    assert len(calls) == 1  # single adapter call for the whole batch

    # Empty scope batch short-circuits BEFORE any source for the new mode too.
    calls2: list[dict[str, Any]] = []
    monkeypatch.setattr(
        svc,
        "compute_current_static_historical_ew_batch",
        _make_ew_source(days, ew_by_scope={}, calls=calls2),
    )
    out2 = asyncio.run(
        svc.compute_current_static_scope_dynamics_batch(
            object(), SCOPE_TYPE, [], list(days),
            analysis_asof_date=days[-1],
            historical_source="columnar_ew",
        )
    )
    assert out2 == []
    assert calls2 == []


def test_d1_old_reconstruction_still_reachable(monkeypatch) -> None:
    """D1-10. The legacy owner is still fully reachable by explicit selector:
    explicit ``"reconstruction"`` runs the exact legacy path (reconstruction
    adapter called once, EW adapter never), keeping the rollback option intact."""
    days = _trading_days(date(2026, 1, 5), 30)
    payload_builder = _fixed_payloads(days, returns_per_index=lambda i: 0.01)
    recon_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        svc,
        "reconstruct_scope_series_batch",
        _make_source(days, payload_builder=payload_builder, calls=recon_calls),
    )

    def _ew_must_not_run(*args, **kwargs):
        raise AssertionError("EW adapter must not run for reconstruction source")

    monkeypatch.setattr(svc, "compute_current_static_historical_ew_batch", _ew_must_not_run)

    out = asyncio.run(_run_ew([SCOPE_KEY], days, historical_source="reconstruction"))
    assert len(out) == 1
    assert len(recon_calls) == 1
    points = out[0]["observation_series"]["primitives"]["equal_weight_return"]["points"]
    assert len(points) == len(days)
    # Legacy provenance layout untouched: no source marker is injected and the
    # reconstruction phase cost is still reported.
    assert "historical_source" not in out[0]["metrics"]
    assert out[0]["metrics"]["batch_reconstruction_ms"] > 0.0
