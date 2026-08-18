"""Pure/unit tests for current-universe historical Scope Observation reconstruction.

Covers the fixed current-static membership contract (Review v2.3):

- Test 1 — current membership is fixed across historical dates
- Test 2 — historical / PIT membership is never consulted
- Test 3 — member facts are read at the exact historical T (never the current day)
- Test 4 — current-only facts stay unavailable for historical T (no backfill)
- Test 5 — a current member missing at T is excluded; provided count drops (no fake 0)
- Test 6 — the final observation comes from ``compute_scope_observation``
- Test 7 — determinism (same inputs -> identical result)

No DB, no network.  All DB-touching helpers are mocked.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.domain.review.scope_observation import MemberObservation
from app.services import review_historical_scope_reconstruction_service as reconstruct
from app.services.observation_prep import RawMemberFacts, build_member_observation
from app.services.review_historical_scope_reconstruction_service import (
    CurrentStaticMembership,
    HistoricalReconstructionError,
    resolve_current_membership,
    resolve_current_memberships_batch,
)
from app.services.review_observation_prep_service import PreparedScope

T1 = date(2026, 8, 3)
T2 = date(2026, 8, 4)


def _flat(trend: str = "上行") -> dict:
    return {
        "fp_trend_direction": trend,
        "fp_swing_direction": "上行",
        "fp_internal_direction": "上行",
        "fp_momentum_direction": "扩张",
    }


def _member(mid: str, trend: str = "上行") -> MemberObservation:
    return build_member_observation(
        RawMemberFacts(
            member_id=mid,
            flat_t=_flat(trend),
            close_t=10.0,
            close_t1=9.0,
            amount_t=100.0,
            volume_t=10.0,
            volume_history=(10.0, 20.0),
            amount_history=(10.0, 20.0),
            flat_t1=_flat(trend),
        )
    )


def _prepared(scope_type, scope_key, trade_date, member_ids, members):
    return PreparedScope(
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name="s",
        trade_date=trade_date,
        canonical_t1=None,
        pit_member_ids=tuple(member_ids),
        pit_member_ids_t1=tuple(member_ids),
        members=tuple(members),
        t1_membership_available=True,
        pit_status_t="current_static",
        pit_status_t1="current_static",
        diagnostics=(),
        event_coverage_member_ids=None,
        events=(),
    )


class _FakeSession:
    """Stand-in AsyncSession (service tests never touch a real DB)."""


# ---------------------------------------------------------------------------
# Test 1 — current membership is fixed across historical dates
# ---------------------------------------------------------------------------


def test_current_membership_fixed_across_dates(monkeypatch) -> None:
    mem = CurrentStaticMembership(
        member_ids=(uuid.uuid4(), uuid.uuid4(), uuid.uuid4()),
        scope_name="电子",
        asof_date=date(2026, 8, 14),
        member_count=3,
    )
    seen: list[tuple[date, tuple]] = []

    async def fake_prepare_series(
        session, scope_type, scope_key, scope_name, trade_dates, member_ids, **kw
    ):
        for d in trade_dates:
            seen.append((d, tuple(member_ids)))
        return [
            _prepared(
                scope_type,
                scope_key,
                d,
                [str(m) for m in member_ids],
                [_member(str(m)) for m in member_ids],
            )
            for d in trade_dates
        ]

    async def fake_resolve(session, scope_type, scope_key, *, asof_date):
        return mem

    monkeypatch.setattr(reconstruct, "resolve_current_membership", fake_resolve)
    monkeypatch.setattr(
        reconstruct, "prepare_scope_series_from_member_ids", fake_prepare_series
    )

    async def scenario():
        return await reconstruct.reconstruct_scope_series(
            _FakeSession(),
            "industry_l1",
            str(uuid.uuid4()),
            [T1, T2],
            asof_date=date(2026, 8, 14),
        )

    out = asyncio.run(scenario())
    assert [d for d, _ in seen] == [T1, T2]
    # Both dates use the SAME current member set.
    assert seen[0][1] == mem.member_ids
    assert seen[1][1] == mem.member_ids
    assert out["membership"]["mode"] == "current_static"
    assert out["membership"]["member_count"] == 3
    assert len(out["series"]) == 2


# ---------------------------------------------------------------------------
# Test 2 — historical / PIT membership is never consulted
# ---------------------------------------------------------------------------


def test_historical_membership_never_consulted(monkeypatch) -> None:
    mem = CurrentStaticMembership(
        member_ids=(uuid.uuid4(),),
        scope_name="s",
        asof_date=date(2026, 8, 14),
        member_count=1,
    )
    pit_calls: list = []

    async def fake_pit_resolver(session, scope_type, scope_key, *, trade_date):
        pit_calls.append((scope_type, scope_key, trade_date))
        return ([uuid.uuid4()], "historical-different")

    async def fake_prepare_series(
        session, scope_type, scope_key, scope_name, trade_dates, member_ids, **kw
    ):
        return [
            _prepared(
                scope_type,
                scope_key,
                d,
                [str(m) for m in member_ids],
                [_member(str(m)) for m in member_ids],
            )
            for d in trade_dates
        ]

    async def fake_resolve(session, scope_type, scope_key, *, asof_date):
        return mem

    monkeypatch.setattr(reconstruct, "resolve_current_membership", fake_resolve)
    monkeypatch.setattr(
        reconstruct, "prepare_scope_series_from_member_ids", fake_prepare_series
    )
    # The historical PIT membership owner must NOT be reached by reconstruction.
    monkeypatch.setattr(
        "app.services.review_scope_service.resolve_scope_members",
        fake_pit_resolver,
    )

    async def scenario():
        return await reconstruct.reconstruct_scope_series(
            _FakeSession(),
            "concept",
            str(uuid.uuid4()),
            [T1],
            asof_date=date(2026, 8, 14),
        )

    out = asyncio.run(scenario())
    assert pit_calls == []  # historical membership resolver never invoked
    assert out["membership"]["mode"] == "current_static"


# ---------------------------------------------------------------------------
# Test 3 — member facts are read at the exact historical T
# ---------------------------------------------------------------------------


def test_member_facts_date_exact(monkeypatch) -> None:
    mem = CurrentStaticMembership(
        member_ids=(uuid.uuid4(),),
        scope_name="s",
        asof_date=date(2026, 8, 14),
        member_count=1,
    )
    dates_seen: list[date] = []

    async def fake_prepare_series(
        session, scope_type, scope_key, scope_name, trade_dates, member_ids, **kw
    ):
        dates_seen.extend(trade_dates)
        return [
            _prepared(
                scope_type,
                scope_key,
                d,
                [str(m) for m in member_ids],
                [_member(str(m)) for m in member_ids],
            )
            for d in trade_dates
        ]

    async def fake_resolve(session, scope_type, scope_key, *, asof_date):
        return mem

    monkeypatch.setattr(reconstruct, "resolve_current_membership", fake_resolve)
    monkeypatch.setattr(
        reconstruct, "prepare_scope_series_from_member_ids", fake_prepare_series
    )

    async def scenario():
        return await reconstruct.reconstruct_scope_series(
            _FakeSession(),
            "industry_l3",
            str(uuid.uuid4()),
            [T1, T2],
            asof_date=date(2026, 8, 14),
        )

    asyncio.run(scenario())
    # The exact requested dates are passed to the batch prep, never the current day.
    assert dates_seen == [T1, T2]


# ---------------------------------------------------------------------------
# Test 4 — current-only facts stay unavailable for historical T (no backfill)
# ---------------------------------------------------------------------------

import app.services.review_observation_prep_service as prep_service  # noqa: E402


async def _install_prep_mocks(
    monkeypatch,
    *,
    t1: date,
    states_t=None,
    states_t1=None,
    bar_facts=None,
    t1_bar_facts=None,
    current_only=None,
) -> None:
    async def _fake_previous(session, ref_date):
        return t1

    async def _fake_load_states(session, ids, trade_date):
        if trade_date == T1:
            return states_t or {}
        return states_t1 or {}

    async def _fake_load_bar_facts(session, ids, trade_date):
        if trade_date == T1:
            return bar_facts or {}
        return t1_bar_facts or {}

    async def _fake_load_structure_events(session, ids, trade_date):
        return []

    async def _fake_load_current_only(session, ids, trade_date):
        # Historical T has NO current snapshot -> the current-only loader is called
        # at T but returns nothing (no latest-snapshot fallback).
        return current_only or {}

    async def _fake_load_coverage(session, ids, trade_date):
        # ROUND-2.2B: historical reconstruction tests carry no event lineage.
        return None

    monkeypatch.setattr(
        "app.services.calendar_service.get_previous_trading_day_async",
        _fake_previous,
    )
    monkeypatch.setattr(prep_service, "_load_states", _fake_load_states)
    monkeypatch.setattr(prep_service, "_load_bar_facts", _fake_load_bar_facts)
    monkeypatch.setattr(prep_service, "_load_structure_events", _fake_load_structure_events)
    monkeypatch.setattr(
        prep_service,
        "_load_backfill_event_coverage_member_ids",
        _fake_load_coverage,
    )
    monkeypatch.setattr(
        prep_service,
        "_load_current_only_snapshot_facts",
        _fake_load_current_only,
    )


def test_current_only_facts_unavailable_for_historical_t(monkeypatch) -> None:
    id_a = uuid.uuid4()
    state = {"regime_value": 1, "swing_bias": 1, "internal_bias": 0, "sqzmom_val": 1.0}

    def _bar(inst, d, close, amount=100.0, volume=10.0):
        return SimpleNamespace(
            trade_date=d,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=volume,
            amount=amount,
        )

    async def scenario():
        await _install_prep_mocks(
            monkeypatch,
            t1=date(2026, 7, 31),
            states_t={id_a: state},
            states_t1={id_a: state},
            bar_facts={id_a: [_bar(id_a, T1, 10.0)]},
            t1_bar_facts={id_a: [_bar(id_a, date(2026, 7, 31), 9.0)]},
            current_only={},  # no current snapshot at historical T
        )
        return await prep_service.prepare_scope_from_member_ids(
            _FakeSession(),
            "concept",
            "k",
            "s",
            T1,
            [id_a],
        )

    prep = asyncio.run(scenario())
    member = prep.members[0]
    # Current-only facts have no historical source -> unavailable (None), never a
    # current-day backfill.
    assert member.bb_position is None
    assert member.bb_width is None
    assert member.release_volume_ratio is None
    assert member.vwap_ret_total is None
    assert member.trailing_top_pct is None


# ---------------------------------------------------------------------------
# Test 5 — a current member missing at T is excluded (no fake 0)
# ---------------------------------------------------------------------------


def test_missing_member_historical_fact_excluded(monkeypatch) -> None:
    id_a, id_b, id_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    state = {"regime_value": 1, "swing_bias": 1, "internal_bias": 0, "sqzmom_val": 1.0}

    def _bar(inst, d, close, amount=100.0, volume=10.0):
        return SimpleNamespace(
            trade_date=d,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=volume,
            amount=amount,
        )

    async def scenario():
        await _install_prep_mocks(
            monkeypatch,
            t1=date(2026, 7, 31),
            # A and B have FP state at T; C (current member) does NOT.
            states_t={id_a: state, id_b: state},
            states_t1={id_a: state, id_b: state},
            bar_facts={
                id_a: [_bar(id_a, T1, 10.0)],
                id_b: [_bar(id_b, T1, 8.0)],
            },
            t1_bar_facts={
                id_a: [_bar(id_a, date(2026, 7, 31), 9.0)],
                id_b: [_bar(id_b, date(2026, 7, 31), 8.0)],
            },
        )
        prepared = await prep_service.prepare_scope_from_member_ids(
            _FakeSession(),
            "concept",
            "k",
            "s",
            T1,
            [id_a, id_b, id_c],
        )
        from app.domain.review.scope_observation import compute_scope_observation

        return compute_scope_observation(
            scope_type="concept",
            scope_key="k",
            trade_date=T1,
            pit_member_ids=prepared.pit_member_ids,
            pit_member_ids_t1=prepared.pit_member_ids_t1,
            members=prepared.members,
            event_coverage_member_ids=prepared.event_coverage_member_ids,
        )

    out = asyncio.run(scenario())
    # ROUND-2 GAP-L1-MEMBER-GATE: C is a current (PIT) member -> provided even
    # though it has no state/bars at T; its facts are None (no fake 0).
    assert out["scope"]["pit_member_count"] == 3
    assert out["scope"]["provided_member_count"] == 3


# ---------------------------------------------------------------------------
# Test 6 — final observation comes from compute_scope_observation (canonical owner)
# ---------------------------------------------------------------------------


def test_final_observation_from_canonical_owner(monkeypatch) -> None:
    mem = CurrentStaticMembership(
        member_ids=(uuid.uuid4(),),
        scope_name="s",
        asof_date=date(2026, 8, 14),
        member_count=1,
    )
    real_compute = reconstruct.compute_scope_observation
    calls: list = []

    def spy_compute(**kw):
        calls.append(kw)
        return real_compute(**kw)

    async def fake_prepare(
        session, scope_type, scope_key, scope_name, trade_date, member_ids, **kw
    ):
        return _prepared(
            scope_type,
            scope_key,
            trade_date,
            [str(m) for m in member_ids],
            [_member(str(m)) for m in member_ids],
        )

    async def fake_resolve(session, scope_type, scope_key, *, asof_date):
        return mem

    monkeypatch.setattr(reconstruct, "resolve_current_membership", fake_resolve)
    monkeypatch.setattr(reconstruct, "prepare_scope_from_member_ids", fake_prepare)
    monkeypatch.setattr(reconstruct, "compute_scope_observation", spy_compute)

    async def scenario():
        return await reconstruct.reconstruct_scope_observation(
            _FakeSession(),
            "industry_l3",
            str(uuid.uuid4()),
            T1,
            mem,
        )

    rec = asyncio.run(scenario())
    assert len(calls) == 1
    # The payload is the canonical structure produced by compute_scope_observation.
    from app.services.review_observation_persistence_service import (
        CANONICAL_TOP_LEVEL_SECTIONS,
    )

    assert set(rec.observation) == CANONICAL_TOP_LEVEL_SECTIONS
    assert "breadth" in rec.observation["price"]
    assert "concentration" in rec.observation["price"]


# ---------------------------------------------------------------------------
# Test 7 — determinism
# ---------------------------------------------------------------------------


def test_determinism(monkeypatch) -> None:
    mem = CurrentStaticMembership(
        member_ids=(uuid.uuid4(), uuid.uuid4()),
        scope_name="s",
        asof_date=date(2026, 8, 14),
        member_count=2,
    )

    async def fake_prepare(
        session, scope_type, scope_key, scope_name, trade_date, member_ids, **kw
    ):
        return _prepared(
            scope_type,
            scope_key,
            trade_date,
            [str(m) for m in member_ids],
            [_member(str(m)) for m in member_ids],
        )

    async def fake_resolve(session, scope_type, scope_key, *, asof_date):
        return mem

    monkeypatch.setattr(reconstruct, "resolve_current_membership", fake_resolve)
    monkeypatch.setattr(reconstruct, "prepare_scope_from_member_ids", fake_prepare)

    async def scenario():
        return await reconstruct.reconstruct_scope_observation(
            _FakeSession(),
            "concept",
            "k",
            T1,
            mem,
        )

    rec1 = asyncio.run(scenario())
    rec2 = asyncio.run(scenario())
    assert rec1.observation == rec2.observation


# ---------------------------------------------------------------------------
# Test 8 — historical reconstruction never fetches current-only snapshot facts
# ---------------------------------------------------------------------------


def test_historical_reconstruction_skips_current_only_loader(monkeypatch) -> None:
    """The reconstruction must pass ``load_current_only=False`` so the
    Current-only snapshot loader (large summary_payload JSONB) is never invoked
    for historical T and current-only facts stay None (PRD v2.3)."""
    mem = CurrentStaticMembership(
        member_ids=(uuid.uuid4(),),
        scope_name="s",
        asof_date=date(2026, 8, 14),
        member_count=1,
    )
    prep_kwargs: dict = {}

    async def fake_prepare(
        session, scope_type, scope_key, scope_name, trade_date, member_ids, **kw
    ):
        prep_kwargs.update(kw)
        return _prepared(
            scope_type,
            scope_key,
            trade_date,
            [str(m) for m in member_ids],
            [_member(str(m)) for m in member_ids],
        )

    async def fake_resolve(session, scope_type, scope_key, *, asof_date):
        return mem

    monkeypatch.setattr(reconstruct, "resolve_current_membership", fake_resolve)
    monkeypatch.setattr(reconstruct, "prepare_scope_from_member_ids", fake_prepare)

    async def scenario():
        return await reconstruct.reconstruct_scope_observation(
            _FakeSession(),
            "concept",
            str(uuid.uuid4()),
            T1,
            mem,
        )

    asyncio.run(scenario())
    assert prep_kwargs.get("load_current_only") is False


# ---------------------------------------------------------------------------
# Current membership resolution: validation + dedupe
# ---------------------------------------------------------------------------


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)

    def all(self):
        return list(self._rows)


class _MembershipRows:
    """Result for the batch memberships query: .all() -> [(board_id, instrument_id)]."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _BoardSession:
    """Mocks the 2-query batch resolver: call1 = boards.scalars(), call2 = memberships.all()."""

    def __init__(self, board, member_ids):
        self._board = board
        self._member_ids = member_ids
        self._calls = 0

    async def execute(self, stmt):
        self._calls += 1
        if self._calls == 1:
            return _ScalarsResult([self._board])
        return _MembershipRows(
            [(self._board.id, iid) for iid in self._member_ids]
        )


def test_resolve_current_membership_validates_and_dedupes() -> None:
    board = SimpleNamespace(
        id=uuid.uuid4(),
        type="industry",
        hierarchyLevel="L1",
        name="电子",
    )
    a, b = uuid.uuid4(), uuid.uuid4()
    session = _BoardSession(board, [a, b, a])  # duplicate a in raw rows
    mem = asyncio.run(
        resolve_current_membership(
            session,
            "industry_l1",
            str(board.id),
            asof_date=date(2026, 8, 14),
        )
    )
    assert mem.scope_name == "电子"
    assert mem.member_count == 2
    assert mem.member_ids == (a, b)  # deduped, order preserved


def test_resolve_current_membership_rejects_type_mismatch() -> None:
    board = SimpleNamespace(
        id=uuid.uuid4(),
        type="concept",
        hierarchyLevel="L1",
        name="概念",
    )
    session = _BoardSession(board, [])
    with pytest.raises(HistoricalReconstructionError):
        asyncio.run(
            resolve_current_membership(
                session,
                "industry_l1",
                str(board.id),
                asof_date=date(2026, 8, 14),
            )
        )


class _MultiBoardSession:
    """Mock the 2-query batch resolver for MANY boards: call1 = boards.scalars(),
    call2 = memberships.all().  ``calls`` counts SQL executions (must stay == 2)."""

    def __init__(self, boards):
        self._boards = boards  # list of SimpleNamespace(id, type, hierarchyLevel, name)
        self._calls = 0
        self._members_by_board = {
            b.id: [uuid.uuid4() for _ in range(2)] for b in boards
        }

    async def execute(self, stmt):
        self._calls += 1
        if self._calls == 1:
            return _ScalarsResult(self._boards)
        return _MembershipRows(
            [
                (bid, iid)
                for bid, mids in self._members_by_board.items()
                for iid in mids
            ]
        )


def test_resolve_current_memberships_batch_two_queries_parity() -> None:
    """PERF-FIX-STRUCTURAL-1 (P0-A): batch resolver uses exactly 2 SQL for N scopes,
    and each scope's membership matches the single resolver's output (parity)."""
    boards = [
        SimpleNamespace(
            id=uuid.uuid4(), type="concept", hierarchyLevel=None, name=f"c{i}",
        )
        for i in range(8)  # 8 concept scopes -> still exactly 2 queries (not 16)
    ]
    session = _MultiBoardSession(boards)
    scope_keys = [str(b.id) for b in boards]
    result = asyncio.run(
        resolve_current_memberships_batch(
            session, "concept", scope_keys, asof_date=date(2026, 8, 14)
        )
    )
    # N+1 fixed: 8 scopes in exactly 2 SQL round-trips.
    assert session._calls == 2
    assert len(result) == 8
    # each scope's membership is non-empty and its name is set.
    for b in boards:
        m = result[str(b.id)]
        assert m.scope_name == b.name
        assert m.member_count == 2
        # batch membership == single resolver membership (parity, same owner)
        single = asyncio.run(
            resolve_current_membership(
                _BoardSession(b, session._members_by_board[b.id]),
                "concept",
                str(b.id),
                asof_date=date(2026, 8, 14),
            )
        )
        assert single.member_ids == m.member_ids
