"""Pure/unit tests for current-universe historical Scope Observation reconstruction.

[REVIEW-EXECUTION-PATH-CONSOLIDATION] 历史重建已收口为唯一 batch/union owner
``reconstruct_scope_series_batch``（单 scope 也走同一 owner，batch size = 1）；
单 scope 入口 ``reconstruct_scope_series`` / ``reconstruct_scope_observation`` /
``prepare_scope_from_member_ids`` / ``prepare_scope_series_from_member_ids`` 已删除。
本文件所有重建断言均针对 batch owner，且验证其内部契约沿 single 语义保持：

- Test 1 — current membership is fixed across historical dates (resolved once)
- Test 2 — historical / PIT membership is never consulted
- Test 3 — member facts are read at the exact historical T (never the current day)
- Test 4 — current-only facts stay unavailable for historical T (no backfill)
- Test 5 — a current member missing at T is still provided (no fake 0)
- Test 6 — the final observation comes from ``compute_scope_observation``
- Test 7 — determinism (same inputs -> identical result)
- Test 8 — current-only snapshot loader is never invoked for historical T
- membership resolution: 2-query batch resolver validation + dedupe

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
from app.services import review_observation_prep_service as prep_service
from app.services.observation_prep import RawMemberFacts, build_member_observation
from app.services.review_historical_scope_reconstruction_service import (
    CurrentStaticMembership,
    HistoricalReconstructionError,
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


def _install_membership_only(monkeypatch, memberships: dict[str, CurrentStaticMembership]) -> None:
    """Only mock the batch membership resolver — the real union prep runs below."""

    async def fake_resolve_batch(session, scope_type, scope_keys, *, asof_date):
        return {sk: memberships[sk] for sk in scope_keys}

    monkeypatch.setattr(
        reconstruct, "resolve_current_memberships_batch", fake_resolve_batch
    )


def _install_prep_mocks(monkeypatch, *, all_bars, states, trading_days) -> None:
    """Mock the batch loaders so the REAL union prep builds members (no DB)."""

    async def fake_calendar(session, trade_dates):
        return {
            t: trading_days[trading_days.index(t) - 1]
            if trading_days.index(t) > 0 else None
            for t in trade_dates
        }

    async def fake_batch_states(session, instrument_ids, trade_dates, t1_by_date):
        return {
            d: states.get(d, {})
            for d in set(trade_dates) | set(t1_by_date.values()) if d is not None
        }

    async def fake_batch_bars(session, instrument_ids, trade_dates):
        return {i: all_bars[i] for i in all_bars}

    async def fake_batch_events(session, instrument_ids, trade_dates):
        return {d: [] for d in trade_dates}

    async def fake_batch_coverage(session, instrument_ids, trade_dates):
        return {d: frozenset(instrument_ids) for d in trade_dates}

    monkeypatch.setattr(prep_service, "_load_batch_calendar", fake_calendar)
    monkeypatch.setattr(prep_service, "_load_batch_states", fake_batch_states)
    monkeypatch.setattr(prep_service, "_load_batch_bars", fake_batch_bars)
    monkeypatch.setattr(prep_service, "_load_batch_events", fake_batch_events)
    monkeypatch.setattr(
        prep_service, "_load_batch_backfill_event_coverage", fake_batch_coverage
    )


def _install_reconstruct_mocks(
    monkeypatch,
    *,
    memberships: dict[str, CurrentStaticMembership],
    dates_seen: list[date] | None = None,
    scope_members_seen: list | None = None,
) -> None:
    """Mock the batch reconstruction's full internal chain with canned prep output.

    Used where the PREP internals are not under test (Tests 1/2/3/6/7).  Tests
    4/5/8 instead keep the real union prep (``_install_prep_mocks``) so they can
    prove current-only / missing-member behavior through the actual owner.
    """

    async def fake_union_ctx(session, trade_dates, member_ids, **kw):
        return None

    async def fake_prepare_scopes(
        session, scope_type, trade_dates, scope_members, union_ctx, **kw
    ):
        if dates_seen is not None:
            dates_seen.extend(trade_dates)
        if scope_members_seen is not None:
            scope_members_seen.append(scope_members)
        out: dict[str, list[PreparedScope]] = {}
        for sk, (member_ids, _name) in scope_members.items():
            out[sk] = [
                _prepared(
                    scope_type, sk, d,
                    [str(m) for m in member_ids],
                    [_member(str(m)) for m in member_ids],
                )
                for d in trade_dates
            ]
        return out

    def _stub_compute(scope_type, scope_key, trade_date, pit_member_ids,
                      pit_member_ids_t1, members, events, *,
                      t1_membership_available=True, event_coverage_member_ids=None):
        return {
            "scope": {
                "scope_type": scope_type,
                "scope_key": scope_key,
                "trade_date": trade_date.isoformat(),
                "pit_member_count": len(pit_member_ids),
                "provided_member_count": len(members),
            }
        }

    monkeypatch.setattr(reconstruct, "prepare_union_fact_context", fake_union_ctx)
    monkeypatch.setattr(reconstruct, "prepare_scopes_from_union", fake_prepare_scopes)
    monkeypatch.setattr(reconstruct, "compute_scope_observation", _stub_compute)
    monkeypatch.setattr(
        reconstruct, "validate_scope_observation_payload", lambda *a, **kw: None
    )
    _install_membership_only(monkeypatch, memberships)


def _run_batch(scope_type, scope_keys, dates, asof) -> list[dict]:
    return asyncio.run(
        reconstruct.reconstruct_scope_series_batch(
            _FakeSession(), scope_type, scope_keys, dates, asof_date=asof,
        )
    )


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
    scope_key = str(uuid.uuid4())
    resolve_calls = {"n": 0, "keys": []}

    async def fake_resolve_batch(session, scope_type, scope_keys, *, asof_date):
        resolve_calls["n"] += 1
        resolve_calls["keys"].extend(scope_keys)
        return dict.fromkeys(scope_keys, mem)

    _install_reconstruct_mocks(monkeypatch, memberships={scope_key: mem})
    monkeypatch.setattr(
        reconstruct, "resolve_current_memberships_batch", fake_resolve_batch
    )

    out = _run_batch("industry_l1", [scope_key], [T1, T2], date(2026, 8, 14))

    # Membership resolved exactly once for the whole series (not per date).
    assert resolve_calls["n"] == 1
    assert resolve_calls["keys"] == [scope_key]
    # Both trade dates are reconstructed with the SAME current member set.
    assert [s["trade_date"] for s in out[0]["series"]] == ["2026-08-03", "2026-08-04"]
    assert [s["provided_member_count"] for s in out[0]["series"]] == [3, 3]
    assert out[0]["membership"]["mode"] == "current_static"
    assert out[0]["membership"]["member_count"] == 3


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
    scope_key = str(uuid.uuid4())
    pit_calls: list = []

    async def fake_pit_resolver(session, scope_type, scope_key, *, trade_date):
        pit_calls.append((scope_type, scope_key, trade_date))
        return ([uuid.uuid4()], "historical-different")

    _install_reconstruct_mocks(monkeypatch, memberships={scope_key: mem})
    # The historical PIT membership owner must NOT be reached by reconstruction.
    monkeypatch.setattr(
        "app.services.review_scope_service.resolve_scope_members",
        fake_pit_resolver,
    )

    out = _run_batch("concept", [scope_key], [T1], date(2026, 8, 14))
    assert pit_calls == []  # historical membership resolver never invoked
    assert out[0]["membership"]["mode"] == "current_static"


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
    scope_key = str(uuid.uuid4())
    dates_seen: list[date] = []
    _install_reconstruct_mocks(
        monkeypatch, memberships={scope_key: mem}, dates_seen=dates_seen,
    )
    _run_batch("industry_l3", [scope_key], [T1, T2], date(2026, 8, 14))
    # The exact requested dates are passed to the batch prep, never the current day.
    assert dates_seen == [T1, T2]


# ---------------------------------------------------------------------------
# Test 4 — current-only facts stay unavailable for historical T (no backfill)
# ---------------------------------------------------------------------------


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


def _to_series(facts):
    from app.services.review_observation_prep_service import _InstrumentBarSeries
    facts = sorted(facts, key=lambda b: b.trade_date)
    return _InstrumentBarSeries(
        facts=tuple(facts), dates=tuple(b.trade_date for b in facts),
    )


def test_current_only_facts_unavailable_for_historical_t(monkeypatch) -> None:
    """Test 4: reconstructing historical T never invokes the Current-only snapshot
    loader (large summary_payload JSONB);  current-only facts stay None, never a
    current-day backfill."""
    id_a = uuid.uuid4()
    state = {"regime_value": 1, "swing_bias": 1, "internal_bias": 0, "sqzmom_val": 1.0}
    mem = CurrentStaticMembership(
        member_ids=(id_a,),
        scope_name="s",
        asof_date=date(2026, 8, 14),
        member_count=1,
    )
    scope_key = str(uuid.uuid4())
    _install_membership_only(monkeypatch, {scope_key: mem})

    bar_facts = {
        id_a: [_bar(id_a, date(2026, 7, 31), 9.0), _bar(id_a, T1, 10.0)],
    }
    states = {d: {id_a: state} for d in (date(2026, 7, 31), T1)}
    trading_days = [date(2026, 7, 31), T1]
    _install_prep_mocks(
        monkeypatch, all_bars={id_a: _to_series(bar_facts[id_a])},
        states=states, trading_days=trading_days,
    )
    invoked = {"current_only": False}

    async def boom(session, instrument_ids, trade_date):
        invoked["current_only"] = True
        raise AssertionError("current-only snapshot loader must not run for historical T")

    monkeypatch.setattr(
        prep_service, "_load_current_only_snapshot_facts", boom,
    )

    out = _run_batch("concept", [scope_key], [T1], date(2026, 8, 14))
    assert invoked["current_only"] is False
    # Series built through the real union prep owner; current-only facts absent.
    member = out[0]["series"][0]
    assert member["provided_member_count"] == 1


# ---------------------------------------------------------------------------
# Test 5 — a current member missing at T is still provided (no fake 0)
# ---------------------------------------------------------------------------


def test_missing_member_historical_fact_still_provided(monkeypatch) -> None:
    id_a, id_b, id_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    state = {"regime_value": 1, "swing_bias": 1, "internal_bias": 0, "sqzmom_val": 1.0}
    mem = CurrentStaticMembership(
        member_ids=(id_a, id_b, id_c),
        scope_name="s",
        asof_date=date(2026, 8, 14),
        member_count=3,
    )
    scope_key = str(uuid.uuid4())
    _install_membership_only(monkeypatch, {scope_key: mem})

    # Real prep owner with id_c missing state at T (still a PIT member).
    bar_facts = {
        id_a: [_bar(id_a, date(2026, 7, 31), 9.0), _bar(id_a, T1, 10.0)],
        id_b: [_bar(id_b, date(2026, 7, 31), 8.0), _bar(id_b, T1, 8.5)],
        id_c: [_bar(id_c, date(2026, 7, 31), 5.0), _bar(id_c, T1, 5.5)],
    }
    states = {
        date(2026, 7, 31): {id_a: state, id_b: state, id_c: state},
        T1: {id_a: state, id_b: state},  # id_c missing state at T
    }
    trading_days = [date(2026, 7, 31), T1]
    _install_prep_mocks(
        monkeypatch,
        all_bars={i: _to_series(bar_facts[i]) for i in bar_facts},
        states=states, trading_days=trading_days,
    )

    out = _run_batch("concept", [scope_key], [T1], date(2026, 8, 14))
    # ROUND-2 GAP-L1-MEMBER-GATE: C is a current (PIT) member -> still provided
    # even though it has no state at T; its facts are None (no fake 0).
    assert out[0]["series"][0]["observation"]["scope"]["pit_member_count"] == 3
    assert out[0]["series"][0]["provided_member_count"] == 3


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
    scope_key = str(uuid.uuid4())
    real_compute = reconstruct.compute_scope_observation
    compute_spy: list = []
    _install_reconstruct_mocks(monkeypatch, memberships={scope_key: mem})

    def spy_compute(**kw):
        compute_spy.append(kw)
        return real_compute(**kw)

    monkeypatch.setattr(reconstruct, "compute_scope_observation", spy_compute)

    out = _run_batch("industry_l3", [scope_key], [T1], date(2026, 8, 14))
    assert len(compute_spy) == 1
    rec = out[0]["series"][0]["observation"]
    # The payload is the canonical structure produced by compute_scope_observation.
    from app.services.review_observation_persistence_service import (
        CANONICAL_TOP_LEVEL_SECTIONS,
    )

    assert set(rec.keys()) >= CANONICAL_TOP_LEVEL_SECTIONS


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
    scope_key = str(uuid.uuid4())
    _install_reconstruct_mocks(monkeypatch, memberships={scope_key: mem})

    one = _run_batch("concept", [scope_key], [T1], date(2026, 8, 14))
    two = _run_batch("concept", [scope_key], [T1], date(2026, 8, 14))
    assert one == two


# ---------------------------------------------------------------------------
# Test 8 — historical reconstruction never fetches current-only snapshot facts
# ---------------------------------------------------------------------------


def test_historical_reconstruction_skips_current_only_loader(monkeypatch) -> None:
    """The reconstruction routes through the historical/union path only, so the
    Current-only snapshot loader (large summary_payload JSONB) is never invoked
    for historical T and current-only facts stay None (PRD v2.3)."""
    id_a = uuid.uuid4()
    state = {"regime_value": 1, "swing_bias": 1, "internal_bias": 0, "sqzmom_val": 1.0}
    mem = CurrentStaticMembership(
        member_ids=(id_a,), scope_name="s",
        asof_date=date(2026, 8, 14), member_count=1,
    )
    scope_key = str(uuid.uuid4())
    _install_membership_only(monkeypatch, {scope_key: mem})
    bar_facts = {
        id_a: [_bar(id_a, date(2026, 7, 31), 9.0), _bar(id_a, T1, 10.0)],
    }
    _install_prep_mocks(
        monkeypatch,
        all_bars={id_a: _to_series(bar_facts[id_a])},
        states={d: {id_a: state} for d in (date(2026, 7, 31), T1)},
        trading_days=[date(2026, 7, 31), T1],
    )
    invoked = {"current_only": 0}

    async def boom(session, instrument_ids, trade_date):
        invoked["current_only"] += 1
        raise AssertionError("current-only snapshot loader must not run")

    monkeypatch.setattr(
        prep_service, "_load_current_only_snapshot_facts", boom,
    )
    _run_batch("concept", [scope_key], [T1], date(2026, 8, 14))
    assert invoked["current_only"] == 0


# ---------------------------------------------------------------------------
# Current membership resolution (batch owner): validation + dedupe
# ---------------------------------------------------------------------------


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


class _MembershipRows:
    """Result for the batch memberships query: .all() -> [(board_id, instrument_id)]."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _MultiBoardSession:
    """Mock the 2-query batch resolver: call1 = boards.scalars(), call2 = memberships.all()."""

    def __init__(self, boards, with_dup=False):
        self._boards = boards  # list of SimpleNamespace(id, type, hierarchyLevel, name)
        self._calls = 0
        self._members_by_board = {
            b.id: [uuid.uuid4() for _ in range(2)] for b in boards
        }
        # Optionally inject a duplicate row for the FIRST board.
        if with_dup and boards:
            first = list(self._members_by_board)[0]
            self._members_by_board[first] = [
                *self._members_by_board[first], self._members_by_board[first][0]
            ]

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
    and each scope's membership is validated + deduped (order preserved)."""
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
    for b in boards:
        m = result[str(b.id)]
        assert m.scope_name == b.name
        assert m.member_count == 2


def test_resolve_current_membership_rejects_type_mismatch() -> None:
    board = SimpleNamespace(
        id=uuid.uuid4(), type="concept", hierarchyLevel="L1", name="概念",
    )
    session = _MultiBoardSession([board])
    with pytest.raises(HistoricalReconstructionError):
        asyncio.run(
            resolve_current_memberships_batch(
                session, "industry_l1", [str(board.id)], asof_date=date(2026, 8, 14),
            )
        )


def test_resolve_current_memberships_batch_dedupes() -> None:
    """A duplicate member row in the membership query is deduped (order preserved)."""
    boards = [
        SimpleNamespace(
            id=uuid.uuid4(), type="industry", hierarchyLevel="L1", name="电子",
        )
    ]
    session = _MultiBoardSession(boards, with_dup=True)
    result = asyncio.run(
        resolve_current_memberships_batch(
            session, "industry_l1", [str(boards[0].id)], asof_date=date(2026, 8, 14),
        )
    )
    m = result[str(boards[0].id)]
    assert m.member_count == 2  # the injected duplicate third row was deduped
    assert m.member_ids == tuple(dict.fromkeys(m.member_ids))
